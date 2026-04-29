"""Concept-graph-driven retrieval pipeline.

Replaces the old interpretation-routed / PPR pipeline. The new model queries
raw chunks directly, uses the concept graph (aspect labels + co_aspect /
cites edges) to expand and rerank, and returns resource-grouped results.

Pipeline:

    1. expand_query_aspects()  → expanded aspect label list from concept graph
    2. HyDE                    → hypothetical document, embed it
    3. search_lex(query, filter_aspects=expanded_aspects)
    4. search_vec(hyde_vec, filter_aspects=expanded_aspects)
    5. RRF fusion              → merged chunk ranking (k=60)
    6. Graph rerank            → boost resources connected by co_aspect / cites
                                  edges to the current top-5
    7. Group by resource_id    → take top chunks per resource
    8. Build RetrievalResult   → with why_surfaced strings
    9. Return top n resources

Score conventions
-----------------
- lex scores from BM25 are negative (smaller = better). We negate them before
  feeding into RRF so that rank 1 = best.
- vec scores are L2 distances (smaller = better). We negate them too.
- RRF formula: 1 / (k + rank), higher = better. k=60 is the canonical default.
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
from loci.retrieve.concept_expand import build_why_surfaced, expand_query_aspects
from loci.retrieve.lex import search_lex
from loci.retrieve.vec import search_vec

log = logging.getLogger(__name__)

# RRF smoothing constant (canonical IR default).
_RRF_K = 60

# Graph rerank: consider edges from the top-N resources.
_GRAPH_RERANK_TOP_N = 5

# Graph rerank multiplicative score boost for a neighbor resource.
_GRAPH_RERANK_BOOST = 1.2

# Edge types used for graph reranking.
_GRAPH_EDGE_TYPES = ["co_aspect", "cites"]

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


async def retrieve(
    query: str,
    project_id: str,
    conn: sqlite3.Connection,
    n: int = 5,
    filter_aspects: list[str] | None = None,
    filter_folder: str | None = None,
    embedder: Embedder | None = None,
) -> list[RetrievalResult]:
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
    # ------------------------------------------------------------------
    hyde_vec: np.ndarray | None = None
    try:
        hypothetical = hyde_mod.hypothesize(query)
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
    # ------------------------------------------------------------------
    lex_results = search_lex(
        query=query,
        project_id=project_id,
        conn=conn,
        limit=20,
        filter_aspects=merged_aspects,
        filter_folder=filter_folder,
    )

    vec_results: list[dict] = []
    if hyde_vec is not None:
        vec_results = search_vec(
            query_vec=hyde_vec,
            project_id=project_id,
            conn=conn,
            limit=20,
            filter_aspects=merged_aspects,
            filter_folder=filter_folder,
        )

    # ------------------------------------------------------------------
    # Step 5: RRF fusion
    # ------------------------------------------------------------------
    # BM25 is already ordered smallest-first (most negative = best).
    # vec is ordered smallest-first (lowest distance = best).
    # RRF rank 1 = best, so we use them in their natural order.

    # Build a combined index keyed by chunk_id.
    # Store per-chunk: resource_id, text, section, raw lex/vec scores.
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

    # Sort chunks by RRF score descending.
    sorted_chunks = sorted(chunk_index.values(), key=lambda c: -c["rrf"])

    # ------------------------------------------------------------------
    # Step 6: Graph rerank
    # ------------------------------------------------------------------
    # Collect the top-N unique resources from the RRF ranking.
    top_resource_ids: list[str] = []
    seen_for_top: set[str] = set()
    for c in sorted_chunks:
        rid = c["resource_id"]
        if rid not in seen_for_top:
            seen_for_top.add(rid)
            top_resource_ids.append(rid)
        if len(top_resource_ids) >= _GRAPH_RERANK_TOP_N:
            break

    # Find neighbor resources connected via co_aspect / cites edges.
    edge_repo = ConceptEdgeRepository(conn)
    boosted_resources: set[str] = set()
    for rid in top_resource_ids:
        neighbors = edge_repo.neighbors(rid, edge_types=_GRAPH_EDGE_TYPES, depth=1)
        boosted_resources.update(neighbors)
    # Don't boost the top resources themselves — only their neighbors.
    boosted_resources -= set(top_resource_ids)

    # Apply boost: multiply the RRF score of chunks from boosted resources.
    for chunk in chunk_index.values():
        if chunk["resource_id"] in boosted_resources:
            chunk["rrf"] *= _GRAPH_RERANK_BOOST

    # Re-sort after boost.
    sorted_chunks = sorted(chunk_index.values(), key=lambda c: -c["rrf"])

    # ------------------------------------------------------------------
    # Step 7: Group by resource, take top chunks per resource
    # ------------------------------------------------------------------
    resource_chunks: dict[str, list[dict]] = {}
    resource_score: dict[str, float] = {}

    for chunk in sorted_chunks:
        rid = chunk["resource_id"]
        bucket = resource_chunks.setdefault(rid, [])
        if len(bucket) < _MAX_CHUNKS_PER_RESOURCE:
            bucket.append(chunk)
        # Resource score = sum of RRF scores of its top chunks.
        resource_score[rid] = resource_score.get(rid, 0.0) + chunk["rrf"]

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

        # Fetch aspects for this resource.
        resource_aspect_rows = aspect_repo.aspects_for(resource_id)
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
            matched_aspects=expanded_aspects,
            conn=conn,
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
