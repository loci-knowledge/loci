"""Concept-graph-driven retrieval pipeline.

Replaces the old interpretation-routed / PPR pipeline. The new model queries
raw chunks directly, uses the concept graph (aspect labels + co_aspect /
cites edges) to expand and rerank, and returns resource-grouped results.

Pipeline (v2.1)
---------------
    1. expand_query_aspects()  → expanded aspect label list via graph
    2. HyDE(q, profile_md, aspects) → hypothetical document, embed it
    3. search_lex(query, filter_aspects=caller_filter_only)
    4. search_vec(hyde_vec, filter_aspects=caller_filter_only)
    5. aspect_overlap_rank()   → third soft channel ranked by aspect confidence
    6. 3-way RRF fusion        → merged chunk ranking (k=60)
    7. Graph rerank (signed-sum, edge-weight-aware)
    8. Aspect-density resource score bonus (α=0.2)
    9. Group by resource_id    → take top chunks per resource
   10. Build RetrievalResult   → with why_surfaced strings, return top n

Score conventions
-----------------
- lex scores from BM25 are negative (smaller = better). We negate them before
  feeding into RRF so that rank 1 = best.
- vec scores are L2 distances (smaller = better). We negate them too.
- RRF formula: 1 / (k + rank), higher = better. k=60 is the canonical default.

Aspect channel (v2.1 addition)
-------------------------------
expanded_aspects are NO LONGER passed as a hard JOIN filter to lex/vec.
Instead they drive a third RRF channel via aspect_overlap_rank(), which
scores resources by sum(confidence) for matching aspects. Zero-recall failure
from an empty expansion is eliminated: lex/vec always scan the full project.
Only explicit caller-supplied filter_aspects remain as hard constraints.

Graph rerank (v2.1 change)
--------------------------
Replaced max-pool with a signed sum weighted by concept_edges.weight:
  boost(r') = 1 + clamp(Σ_e signed_mult[e.type] * e.weight, -0.5, +0.6)
contradicts edges now actively demote when no stronger positive edge exists.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

import numpy as np

from loci.embed.local import Embedder, get_embedder
from loci.graph.aspects import AspectRepository
from loci.graph.concept_edges import ConceptEdgeRepository
from loci.retrieve import hyde as hyde_mod
from loci.retrieve.concept_expand import (
    aspect_overlap_rank,
    build_why_surfaced,
    expand_query_aspects,
)
from loci.retrieve.lex import search_lex
from loci.retrieve.vec import search_vec

log = logging.getLogger(__name__)

# RRF smoothing constant (canonical IR default).
_RRF_K = 60

# Graph rerank: consider edges from the top-N resources.
_GRAPH_RERANK_TOP_N = 5

# Edge types used for graph reranking.
_GRAPH_EDGE_TYPES = ["co_aspect", "cites", "co_recalled", "supports", "instantiates", "depends_on", "contradicts"]

# Signed per-type delta for boost formula:
#   boost(r') = 1 + clamp(Σ_e DELTA[e.type] * e.weight, -0.5, +0.6)
# Positive = promote; negative = demote.  max-pool replaced by signed sum.
_EDGE_TYPE_DELTA: dict[str, float] = {
    "supports":     +0.30,
    "instantiates": +0.25,
    "cites":        +0.20,
    "depends_on":   +0.20,
    "co_aspect":    +0.15,
    "co_recalled":  +0.10,
    "contradicts":  -0.15,
}

# Aspect-density bonus coefficient (R7).
_ASPECT_DENSITY_ALPHA = 0.2

# Weight for the aspect-overlap RRF channel relative to lex/vec (each weight=1).
_ASPECT_CHANNEL_WEIGHT = 0.5

# Maximum chunks to keep per resource in the final result.
_MAX_CHUNKS_PER_RESOURCE = 3


@dataclass
class ChunkResult:
    """A single chunk included in a RetrievalResult."""

    chunk_id: str
    text: str
    lex_score: float    # raw BM25 score (negative; closer to 0 = better)
    vec_score: float    # raw L2 distance (lower = better); 0.0 if not hit by vec
    section: str | None


@dataclass
class RetrievalResult:
    """A retrieved resource with its top chunks and provenance metadata."""

    resource_id: str
    title: str
    folder: str | None
    aspects: list[str]
    chunks: list[ChunkResult]
    why_surfaced: str
    total_score: float   # higher = better; RRF-fused and graph-boosted


@dataclass
class RetrievalTrace:
    """Pipeline trace returned when retrieve(return_trace=True) is called."""

    expanded_aspects: list[str]
    hyde_hypothesis: str | None
    boosted_resource_ids: frozenset[str]


async def retrieve(
    query: str,
    project_id: str,
    conn: sqlite3.Connection,
    n: int = 5,
    filter_aspects: list[str] | None = None,
    filter_folder: str | None = None,
    embedder: Embedder | None = None,
    return_trace: bool = False,
    cnl_query: "Query | None" = None,  # noqa: F821 — parsed ?clause filter
) -> "list[RetrievalResult] | tuple[list[RetrievalResult], RetrievalTrace]":
    """Run the concept-graph retrieval pipeline.

    Parameters
    ----------
    query:
        The user's search query.
    project_id:
        The project to search within.
    conn:
        An open SQLite connection.
    n:
        Number of top resources to return.
    filter_aspects:
        Optional caller-supplied aspect labels to restrict the search.
        These are merged with the query-expanded aspects.
    filter_folder:
        Optional folder prefix filter (joined via resource_provenance.folder).
    embedder:
        Optional pre-loaded Embedder. If None, the process-global one is used.
    """
    emb = embedder or get_embedder()

    # ------------------------------------------------------------------
    # Step 1: expand query into aspect labels via the concept graph
    # ------------------------------------------------------------------
    expanded_aspects = expand_query_aspects(
        query=query,
        project_id=project_id,
        conn=conn,
        embedder=emb,
        top_k_aspects=5,
    )
    # Merge caller-supplied aspects with graph-expanded ones.
    merged_aspects: list[str] | None = None
    if filter_aspects or expanded_aspects:
        seen: set[str] = set()
        merged_aspects = []
        for label in (filter_aspects or []) + expanded_aspects:
            if label not in seen:
                seen.add(label)
                merged_aspects.append(label)

    log.debug(
        "retrieve: query=%r project=%s expanded_aspects=%s",
        query, project_id, expanded_aspects,
    )

    # ------------------------------------------------------------------
    # Step 2: HyDE — generate a hypothetical doc and embed it
    # Aspects are passed so HyDE is grounded in project vocabulary (R2).
    # ------------------------------------------------------------------
    hyde_vec: np.ndarray | None = None
    hypothetical: str | None = None
    try:
        profile_row = conn.execute(
            "SELECT profile_md FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        project_memo = profile_row["profile_md"] if profile_row and profile_row["profile_md"] else None

        hypothetical = await hyde_mod.hypothesize(
            query,
            project_memo=project_memo,
            aspects=expanded_aspects or None,
        )
        if hypothetical and hypothetical != query:
            hyde_vec = emb.encode(hypothetical)
        else:
            hyde_vec = emb.encode(query)
    except Exception:
        log.warning("HyDE failed; falling back to direct query embedding", exc_info=True)
        try:
            hyde_vec = emb.encode(query)
        except Exception:
            log.error("Query embedding failed; vec search disabled", exc_info=True)
            hyde_vec = None

    # ------------------------------------------------------------------
    # Step 3 & 4: BM25 + ANN search
    # NOTE: only caller-supplied filter_aspects are passed as a hard filter.
    # query-expanded aspects drive the soft third channel instead (Step 5b).
    # ------------------------------------------------------------------
    lex_results = search_lex(
        query=query,
        project_id=project_id,
        conn=conn,
        limit=20,
        filter_aspects=filter_aspects,
        filter_folder=filter_folder,
    )

    vec_results: list[dict] = []
    if hyde_vec is not None:
        vec_results = search_vec(
            query_vec=hyde_vec,
            project_id=project_id,
            conn=conn,
            limit=20,
            filter_aspects=filter_aspects,
            filter_folder=filter_folder,
        )

    # ------------------------------------------------------------------
    # Step 5a: RRF fusion (lex + vec channels)
    # ------------------------------------------------------------------
    chunk_index: dict[str, dict] = {}

    for rank, hit in enumerate(lex_results, start=1):
        cid = hit["chunk_id"]
        if cid not in chunk_index:
            chunk_index[cid] = {
                "chunk_id": cid,
                "resource_id": hit["resource_id"],
                "text": hit["text"],
                "section": hit.get("section"),
                "lex_score": hit["score"],
                "vec_score": 0.0,
                "rrf": 0.0,
            }
        chunk_index[cid]["lex_score"] = hit["score"]
        chunk_index[cid]["rrf"] += 1.0 / (_RRF_K + rank)

    for rank, hit in enumerate(vec_results, start=1):
        cid = hit["chunk_id"]
        if cid not in chunk_index:
            chunk_index[cid] = {
                "chunk_id": cid,
                "resource_id": hit["resource_id"],
                "text": hit["text"],
                "section": hit.get("section"),
                "lex_score": 0.0,
                "vec_score": hit["score"],
                "rrf": 0.0,
            }
        chunk_index[cid]["vec_score"] = hit["score"]
        chunk_index[cid]["rrf"] += 1.0 / (_RRF_K + rank)

    # ------------------------------------------------------------------
    # Step 5b: Aspect-overlap soft channel (R1)
    # Ranks resources by sum(confidence) for expanded aspects, then
    # broadcasts resource rank to all its chunks via a weighted RRF term.
    # ------------------------------------------------------------------
    if expanded_aspects or (cnl_query is not None and not cnl_query.is_empty):
        asp_ranked = aspect_overlap_rank(
            conn=conn,
            project_id=project_id,
            expanded_aspects=expanded_aspects,
            limit=20,
            query=cnl_query,
        )
        # Build resource→rank mapping
        asp_resource_rank = {rid: rank for rank, (rid, _) in enumerate(asp_ranked, start=1)}

        for cid, chunk in chunk_index.items():
            rid = chunk["resource_id"]
            asp_rank = asp_resource_rank.get(rid)
            if asp_rank is not None:
                chunk["rrf"] += _ASPECT_CHANNEL_WEIGHT / (_RRF_K + asp_rank)

        # Also inject any resources from asp_ranked that aren't yet in chunk_index.
        # We need a chunk to attach them to; look them up from raw_chunks.
        existing_resources = {c["resource_id"] for c in chunk_index.values()}
        missing = [(rid, s) for rid, s in asp_ranked if rid not in existing_resources]
        if missing:
            for rid, _asp_score in missing[:10]:
                row = conn.execute(
                    """
                    SELECT rc.id AS chunk_id, rc.text, rc.section
                    FROM raw_chunks rc
                    JOIN nodes n ON n.id = rc.raw_id
                    WHERE rc.raw_id = ? AND n.status IN ('live', 'dirty')
                    ORDER BY rc.seq LIMIT 1
                    """,
                    (rid,),
                ).fetchone()
                if row:
                    asp_rank = asp_resource_rank[rid]
                    cid = row["chunk_id"]
                    if cid not in chunk_index:
                        chunk_index[cid] = {
                            "chunk_id": cid,
                            "resource_id": rid,
                            "text": row["text"] or "",
                            "section": row["section"],
                            "lex_score": 0.0,
                            "vec_score": 0.0,
                            "rrf": _ASPECT_CHANNEL_WEIGHT / (_RRF_K + asp_rank),
                        }

    # Sort chunks by RRF score descending.
    sorted_chunks = sorted(chunk_index.values(), key=lambda c: -c["rrf"])

    # ------------------------------------------------------------------
    # Step 6: Graph rerank (signed-sum, edge-weight-aware) (R3)
    # boost(r') = 1 + clamp(Σ_e DELTA[e.type]*e.weight, -0.5, +0.6)
    # Replaces max-pool; contradicts now actively demotes.
    # ------------------------------------------------------------------
    top_resource_ids: list[str] = []
    seen_for_top: set[str] = set()
    for c in sorted_chunks:
        rid = c["resource_id"]
        if rid not in seen_for_top:
            seen_for_top.add(rid)
            top_resource_ids.append(rid)
        if len(top_resource_ids) >= _GRAPH_RERANK_TOP_N:
            break

    edge_repo = ConceptEdgeRepository(conn)
    neighbor_delta: dict[str, float] = {}  # resource_id → cumulative signed delta
    for rid in top_resource_ids:
        for edge in edge_repo.edges_from(rid, edge_types=_GRAPH_EDGE_TYPES, project_id=project_id):
            delta = _EDGE_TYPE_DELTA.get(edge.edge_type, 0.0) * edge.weight
            neighbor_delta[edge.dst_id] = neighbor_delta.get(edge.dst_id, 0.0) + delta
        for edge in edge_repo.edges_to(rid, edge_types=_GRAPH_EDGE_TYPES, project_id=project_id):
            delta = _EDGE_TYPE_DELTA.get(edge.edge_type, 0.0) * edge.weight
            neighbor_delta[edge.src_id] = neighbor_delta.get(edge.src_id, 0.0) + delta
    # Top resources don't boost themselves.
    for rid in top_resource_ids:
        neighbor_delta.pop(rid, None)

    # Convert delta to multiplicative boost: 1 + clamp(delta, -0.5, +0.6)
    neighbor_boost: dict[str, float] = {
        rid: 1.0 + max(-0.5, min(0.6, delta))
        for rid, delta in neighbor_delta.items()
    }
    boosted_resources: set[str] = set(neighbor_boost.keys())

    for chunk in chunk_index.values():
        boost = neighbor_boost.get(chunk["resource_id"])
        if boost is not None:
            chunk["rrf"] *= boost

    sorted_chunks = sorted(chunk_index.values(), key=lambda c: -c["rrf"])

    # ------------------------------------------------------------------
    # Step 7: Group by resource, take top chunks; aspect-density bonus (R7)
    # score(r) += α * |aspects(r) ∩ ε(q)| / max(|ε(q)|, 1)
    # ------------------------------------------------------------------
    resource_chunks: dict[str, list[dict]] = {}
    resource_score: dict[str, float] = {}

    for chunk in sorted_chunks:
        rid = chunk["resource_id"]
        bucket = resource_chunks.setdefault(rid, [])
        if len(bucket) < _MAX_CHUNKS_PER_RESOURCE:
            bucket.append(chunk)
        resource_score[rid] = resource_score.get(rid, 0.0) + chunk["rrf"]

    # Aspect-density bonus: reward resources covering more expanded aspects.
    # v2.2: field-weighted when cnl_query provides typed constraints.
    if expanded_aspects or (cnl_query is not None and not cnl_query.is_empty):
        aspect_repo_inner = AspectRepository(conn)
        n_expanded = max(len(expanded_aspects), 1)
        expanded_set = set(expanded_aspects)
        q_kind = cnl_query.kind if cnl_query else None
        q_role = cnl_query.role if cnl_query else None
        for rid in resource_score:
            r_aspect_rows = aspect_repo_inner.aspects_for(rid, project_id=project_id)
            overlap_score = 0.0
            for ra in r_aspect_rows:
                r_label = _lookup_label(ra.aspect_id, conn)
                if r_label is None:
                    continue
                if r_label in expanded_set or (cnl_query and not cnl_query.is_empty):
                    # Base match
                    overlap_score += 1.0
                    # Extra credit for typed-field matches
                    aspect_row = conn.execute(
                        "SELECT kind, role FROM aspect_vocab WHERE id = ?", (ra.aspect_id,)
                    ).fetchone()
                    if aspect_row:
                        if q_kind and aspect_row["kind"] == q_kind:
                            overlap_score += 0.5
                        if q_role and aspect_row["role"] == q_role:
                            overlap_score += 0.5
            if overlap_score:
                resource_score[rid] += _ASPECT_DENSITY_ALPHA * overlap_score / n_expanded

    # Sort resources by their aggregate score descending.
    ranked_resources = sorted(
        resource_score.items(), key=lambda kv: -kv[1],
    )

    # ------------------------------------------------------------------
    # Step 8: Build RetrievalResult list
    # ------------------------------------------------------------------
    aspect_repo = AspectRepository(conn)
    results: list[RetrievalResult] = []

    for resource_id, total_score in ranked_resources[:n]:
        # Fetch resource metadata.
        meta = _fetch_resource_meta(conn, resource_id)
        if meta is None:
            continue

        # Fetch aspects for this resource (project-scoped when available).
        resource_aspect_rows = aspect_repo.aspects_for(resource_id, project_id=project_id)
        resource_aspect_labels = [
            _lookup_label(ra.aspect_id, conn)
            for ra in resource_aspect_rows
        ]
        resource_aspect_labels = [label for label in resource_aspect_labels if label]

        # Build ChunkResult list.
        chunk_results = [
            ChunkResult(
                chunk_id=c["chunk_id"],
                text=c["text"],
                lex_score=c["lex_score"],
                vec_score=c["vec_score"],
                section=c.get("section"),
            )
            for c in resource_chunks.get(resource_id, [])
        ]

        # Build why_surfaced explanation using the winning chunk.
        winning_chunk = resource_chunks.get(resource_id, [{}])[0]
        why = build_why_surfaced(
            chunk={"resource_id": resource_id, **winning_chunk},
            matched_aspects=merged_aspects or expanded_aspects,
            conn=conn,
            project_id=project_id,
        )
        # Note if this resource was boosted by the graph.
        if resource_id in boosted_resources:
            why += " (surfaced via concept-graph neighbor)"

        results.append(RetrievalResult(
            resource_id=resource_id,
            title=meta["title"],
            folder=meta.get("folder"),
            aspects=resource_aspect_labels,
            chunks=chunk_results,
            why_surfaced=why,
            total_score=total_score,
        ))

    if return_trace:
        return results, RetrievalTrace(
            expanded_aspects=expanded_aspects,
            hyde_hypothesis=hypothetical,
            boosted_resource_ids=frozenset(boosted_resources),
        )
    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fetch_resource_meta(
    conn: sqlite3.Connection,
    resource_id: str,
) -> dict | None:
    """Return {title, folder} for a resource, or None if not found."""
    row = conn.execute(
        """
        SELECT n.title, rp.folder
        FROM nodes n
        LEFT JOIN resource_provenance rp ON rp.resource_id = n.id
        WHERE n.id = ?
        """,
        (resource_id,),
    ).fetchone()
    if row is None:
        return None
    return {"title": row["title"], "folder": row["folder"]}


def _lookup_label(aspect_id: str, conn: sqlite3.Connection) -> str | None:
    """Look up an aspect label by id."""
    row = conn.execute(
        "SELECT label FROM aspect_vocab WHERE id = ?", (aspect_id,)
    ).fetchone()
    return row["label"] if row else None
