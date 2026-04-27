"""Retrieval orchestrator — interpretation-routed.

The new model: interpretations are LOCI OF THOUGHT, not retrieval targets.
A query never returns interpretation bodies as content. Interpretations route
the query to raws via their `cites` and `derives_from` edges, and the
response surfaces those raws together with the trace path that reached them.

Pipeline:

    1. INTERP STAGE
       - Score interpretations against the query (lex + vec) over the project.
       - PPR over the derives_from DAG, anchored on (caller anchors ∪ pinned
         ∪ top-vec-interp anchors), to weight loci by graph centrality.
       - Fuse via RRF → top-K_interp interpretation handles.

    2. ROUTE STAGE
       - For each top interp, walk:
           cites          → directly anchored raws       (depth 1)
           derives_from·cites → raws of upstream loci    (depth 2, downweighted)
       - Accumulate per-raw provenance: list of (interp_id, edge_type) hops.

    3. DIRECT STAGE
       - Also score raws directly (lex + vec) — if a raw is the right answer
         even without a routing locus, we don't want to miss it.

    4. MERGE
       - Combine routed + direct raw scores. Routed raws get a multiplicative
         provenance bonus proportional to how many top loci point at them and
         how strong those loci scored. The bonus is capped — we don't want
         the agent's loci to drown out raws that genuinely match the query.

    5. RESPONSE
       - `nodes`: ranked raws (no interpretations).
       - `traces`: per-raw, the ordered list of interp hops that routed to it.
       - `routing_interps`: deduplicated interpretation handles used in stage 2,
         carried separately for UI display ("we considered these loci").

Score fusion: same RRF as before within a stage; cross-stage merge uses
weighted sum because the route-bonus is multiplicative and asymmetric.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from loci.embed.local import Embedder, get_embedder
from loci.graph import EdgeRepository, NodeRepository, ProjectRepository
from loci.graph.models import NodeKind, Subkind
from loci.retrieve import hyde as hyde_mod
from loci.retrieve import lex as lex_mod
from loci.retrieve import ppr as ppr_mod
from loci.retrieve import vec as vec_mod

log = logging.getLogger(__name__)

# Reciprocal-Rank-Fusion smoothing constant. 60 is the canonical IR default.
RRF_K = 60

# Channel weights for the interp-stage RRF fusion. Vec leads because the
# locus's relation/overlap/anchor text encodes the bridge semantically; lex
# catches exact-term loci; PPR brings DAG centrality.
INTERP_WEIGHTS: dict[str, float] = {
    "lex": 1.0,
    "vec": 1.5,
    "hyde": 0.8,
    "ppr": 0.7,
}

# Channel weights for the direct-raw RRF fusion. Lex weighs higher than for
# interps because raws are long documents with verbatim terminology.
DIRECT_WEIGHTS: dict[str, float] = {
    "lex": 1.2,
    "vec": 1.2,
    "hyde": 0.6,
}

# Routing parameters — how much the interp layer biases raw retrieval.
ROUTE_DEPTH = 2          # 1 = only direct cites; 2 = cites of upstream loci too
ROUTE_DECAY = 0.5        # multiplicative downweight per derives_from hop
ROUTE_BONUS_CAP = 1.0    # max additive bonus a single raw can receive
ROUTE_BONUS_GAIN = 0.6   # how much of the interp's score becomes a route bonus


@dataclass
class RetrievalRequest:
    project_id: str
    query: str
    k: int = 10
    anchors: list[str] = field(default_factory=list)
    # Filter: caller may restrict to {raw}, {interpretation}, or specific
    # subkinds. Default None = raws only (the new contract — interpretations
    # are routing context, not results). Set explicitly to surface loci.
    include: list[NodeKind | Subkind] | None = None
    hyde: bool = False
    # Channel widths.
    k_lex: int = 30
    k_vec: int = 30
    k_hyde: int = 20
    # Stage-1 (interp) breadth.
    k_interps: int = 20
    # Stage-3 (direct raw) breadth.
    k_direct: int = 30


@dataclass
class RouteHop:
    """One step in a retrieval trace.

    For a `derives_from` step, src and dst are both interp ids.
    For a `cites` step (the terminal hop), src is interp, dst is the raw.
    """
    src: str
    dst: str
    edge_type: str           # 'derives_from' | 'cites'
    interp_score: float = 0.0  # the routing locus's interp-stage score


@dataclass
class RetrievedNode:
    node_id: str
    kind: NodeKind
    subkind: Subkind
    title: str
    snippet: str
    score: float
    why: str
    channel_ranks: dict[str, int] = field(default_factory=dict)
    # Trace through the interp DAG that reached this node. For a raw retrieved
    # via routing, this is the path of hops; for a directly-scored raw, empty.
    trace: list[RouteHop] = field(default_factory=list)
    # When the raw was reached via a chunk-level hit (vec or lex on chunks_fts),
    # these point at the winning span. None for interp hits or for raws that
    # were only routed-via (no direct chunk match) — in that case the snippet
    # falls back to a head truncation of the body.
    chunk_id: str | None = None
    chunk_text: str | None = None
    chunk_section: str | None = None


@dataclass
class RoutingInterp:
    """An interpretation node that was used as a router (not returned as a result)."""
    node_id: str
    subkind: Subkind
    title: str
    relation_md: str
    overlap_md: str
    source_anchor_md: str
    angle: str | None
    score: float
    # Per-channel rank (1-based) for this locus. None / missing channel = the
    # locus did not appear in that channel's top-K. Verbose-mode field.
    channel_ranks: dict[str, int] = field(default_factory=dict)
    # Per-channel RRF contribution (already weighted). Sums to ≤ `score`.
    channel_scores: dict[str, float] = field(default_factory=dict)
    # If this locus was used as a PPR anchor, where did the anchor come from?
    # 'caller' (passed in), 'pinned' (project pin), 'top_vec_interp' (auto).
    # None when the locus wasn't itself an anchor.
    anchor_source: str | None = None


@dataclass
class PrunedLocus:
    """A locus that scored above zero but did NOT route any raw.

    Reasons:
      - 'below_top_k': scored but ranked below `k_interps` cutoff
      - 'no_routing_edges': made the top-K but has neither cites nor
                            derives_from·cites edges, so its walk yielded nothing
    """
    node_id: str
    subkind: Subkind
    title: str
    score: float
    reason: str
    channel_ranks: dict[str, int] = field(default_factory=dict)


@dataclass
class RetrievalResponse:
    nodes: list[RetrievedNode]
    routing_interps: list[RoutingInterp]
    # Compact provenance summary: one row per returned raw with interp ids.
    trace_table: list[dict] = field(default_factory=list)
    # Loci that scored but didn't route a raw — useful for verbose UIs that
    # want to show "we considered this but it had no path to evidence."
    pruned_loci: list[PrunedLocus] = field(default_factory=list)
    # Pre-rendered markdown narrative of the trace — see narrative.py.
    trace_narrative: str = ""


class Retriever:
    def __init__(self, conn: sqlite3.Connection, embedder: Embedder | None = None) -> None:
        self.conn = conn
        self.embedder = embedder or get_embedder()
        self.nodes_repo = NodeRepository(conn)
        self.edges_repo = EdgeRepository(conn)
        self.projects_repo = ProjectRepository(conn)

    def retrieve(self, req: RetrievalRequest) -> RetrievalResponse:
        # --------------------------------------------------------------
        # Stage 1: score interpretations
        # --------------------------------------------------------------
        interp_lex = lex_mod.search(
            self.conn, req.project_id, req.query, k=req.k_lex, kind="interpretation",
        )
        interp_vec = vec_mod.search_text(
            self.conn, req.project_id, req.query, k=req.k_vec,
            embedder=self.embedder, kind="interpretation",
        )
        interp_hyde: list[vec_mod.VecHit] = []
        if req.hyde:
            hypothetical = hyde_mod.hypothesize(req.query)
            if hypothetical and hypothetical != req.query:
                interp_hyde = vec_mod.search_text(
                    self.conn, req.project_id, hypothetical,
                    k=req.k_hyde, embedder=self.embedder, kind="interpretation",
                )

        # PPR over the derives_from DAG of interpretations.
        anchors_tagged = self._resolve_anchors_tagged(req, interp_vec)
        anchor_source_by_id: dict[str, str] = {a: src for a, src in anchors_tagged}
        anchors = [a for a, _src in anchors_tagged]
        ppr_result = ppr_mod.run(self.conn, req.project_id, anchors)
        ppr_ranked = sorted(ppr_result.scores.items(), key=lambda kv: -kv[1])

        interp_scores, interp_channel_scores, interp_channel_ranks = (
            self._fuse_with_breakdown(
                channels=[
                    ("lex", [h.node_id for h in interp_lex]),
                    ("vec", [h.node_id for h in interp_vec]),
                    ("hyde", [h.node_id for h in interp_hyde]),
                    ("ppr", [nid for nid, _ in ppr_ranked]),
                ],
                weights=INTERP_WEIGHTS,
            )
        )
        # Full ranking — keep the tail so we can report "considered but pruned"
        # loci to verbose callers.
        full_interp_ranking = sorted(
            interp_scores.items(), key=lambda kv: -kv[1],
        )
        top_interps = full_interp_ranking[: req.k_interps]
        below_top_k = full_interp_ranking[req.k_interps :]
        interp_score_map = dict(top_interps)

        # --------------------------------------------------------------
        # Stage 2: route through cites / derives_from to raws
        # --------------------------------------------------------------
        routed_scores: dict[str, float] = {}
        per_raw_trace: dict[str, list[RouteHop]] = {}
        for interp_id, locus_score in top_interps:
            self._walk_routes(
                interp_id=interp_id,
                interp_score=locus_score,
                routed_scores=routed_scores,
                per_raw_trace=per_raw_trace,
            )

        # --------------------------------------------------------------
        # Stage 3: directly score raws against the query
        # --------------------------------------------------------------
        raw_lex = lex_mod.search(
            self.conn, req.project_id, req.query, k=req.k_direct, kind="raw",
        )
        raw_vec = vec_mod.search_text(
            self.conn, req.project_id, req.query, k=req.k_direct,
            embedder=self.embedder, kind="raw",
        )
        raw_hyde: list[vec_mod.VecHit] = []
        if req.hyde and interp_hyde is not None:
            hyp = hyde_mod.hypothesize(req.query)
            if hyp and hyp != req.query:
                raw_hyde = vec_mod.search_text(
                    self.conn, req.project_id, hyp,
                    k=req.k_hyde, embedder=self.embedder, kind="raw",
                )
        direct_scores = self._fuse(
            channels=[
                ("lex", [h.node_id for h in raw_lex]),
                ("vec", [h.node_id for h in raw_vec]),
                ("hyde", [h.node_id for h in raw_hyde]),
            ],
            weights=DIRECT_WEIGHTS,
        )

        # --------------------------------------------------------------
        # Stage 4: merge routed + direct
        # --------------------------------------------------------------
        merged: dict[str, float] = {}
        for nid, s in direct_scores.items():
            merged[nid] = merged.get(nid, 0.0) + s
        for nid, s in routed_scores.items():
            merged[nid] = merged.get(nid, 0.0) + min(ROUTE_BONUS_CAP, s)

        # --------------------------------------------------------------
        # Stage 5: materialise + filter
        # --------------------------------------------------------------
        ranked = sorted(merged.items(), key=lambda kv: -kv[1])

        # Per-raw winning chunk: prefer the lex chunk hit (it has snippet
        # markers from FTS), then fall back to the vec chunk hit. Both carry
        # `chunk_id` + `chunk_text` only when the underlying index returned
        # span-level rows.
        snippet_by_id: dict[str, str] = {}
        chunk_by_id: dict[str, tuple[str | None, str | None, str | None]] = {}
        for h in raw_lex:
            snippet_by_id.setdefault(h.node_id, h.snippet)
            if h.chunk_id and h.node_id not in chunk_by_id:
                chunk_by_id[h.node_id] = (h.chunk_id, h.chunk_text, h.chunk_section)
        for h in raw_vec:
            if h.chunk_id and h.node_id not in chunk_by_id:
                chunk_by_id[h.node_id] = (h.chunk_id, h.chunk_text, h.chunk_section)

        node_ids_to_load = [nid for nid, _ in ranked[: req.k * 4]]
        nodes_by_id = {n.id: n for n in self.nodes_repo.get_many(node_ids_to_load)}

        # Default include: raws only — interpretations are routing context.
        include = req.include if req.include else ["raw"]

        materialised: list[RetrievedNode] = []
        for nid, score in ranked:
            node = nodes_by_id.get(nid)
            if node is None:
                continue
            if not _kind_match(node, include):
                continue
            why = self._why(
                node=node,
                routed=nid in routed_scores,
                direct=nid in direct_scores,
                trace=per_raw_trace.get(nid, []),
            )
            chunk_id, chunk_text, chunk_section = chunk_by_id.get(nid, (None, None, None))
            # Snippet preference: FTS snippet (with ⟪…⟫ markers) > raw chunk
            # text > head-truncated body fallback.
            snippet = snippet_by_id.get(nid)
            if not snippet and chunk_text:
                snippet = _snippet_fallback(chunk_text)
            if not snippet:
                snippet = _snippet_fallback(node.body)
            materialised.append(RetrievedNode(
                node_id=nid, kind=node.kind, subkind=node.subkind,
                title=node.title, snippet=snippet,
                score=score, why=why, channel_ranks={},
                trace=per_raw_trace.get(nid, []),
                chunk_id=chunk_id, chunk_text=chunk_text,
                chunk_section=chunk_section,
            ))
            self.nodes_repo.bump_access(nid)
            if len(materialised) >= req.k:
                break

        # --------------------------------------------------------------
        # Build the routing-interp side panel + trace table
        # --------------------------------------------------------------
        used_interp_ids: set[str] = set()
        for r in materialised:
            for hop in r.trace:
                if hop.edge_type == "cites":
                    used_interp_ids.add(hop.src)
                else:
                    used_interp_ids.add(hop.src)
                    used_interp_ids.add(hop.dst)
        routing_interps = self._materialise_routing_interps(
            list(used_interp_ids), interp_score_map,
            channel_scores_by_id=interp_channel_scores,
            channel_ranks_by_id=interp_channel_ranks,
            anchor_source_by_id=anchor_source_by_id,
        )
        trace_table = [
            {
                "raw_id": r.node_id,
                "raw_title": r.title,
                "interp_path": [
                    {"id": hop.src, "edge": hop.edge_type, "to": hop.dst}
                    for hop in r.trace
                ],
            }
            for r in materialised
            if r.kind == "raw"
        ]

        # Pruned loci: top-K loci whose walk yielded zero hops, plus the
        # top scorers immediately below the K_interp cutoff. Verbose-mode
        # output — the renderer trims to the highest-scoring slice.
        pruned_loci = self._materialise_pruned_loci(
            top_interps=top_interps,
            below_top_k=below_top_k,
            used_interp_ids=used_interp_ids,
            interp_channel_ranks=interp_channel_ranks,
        )

        # Build a per-raw markdown narrative once so both retrieve and draft
        # ship the same human-readable trace.
        from loci.retrieve.narrative import render_trace_narrative
        narrative = render_trace_narrative(
            nodes=materialised, routing_interps=routing_interps,
        )

        return RetrievalResponse(
            nodes=materialised,
            routing_interps=routing_interps,
            trace_table=trace_table,
            pruned_loci=pruned_loci,
            trace_narrative=narrative,
        )

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    def _resolve_anchors(
        self, req: RetrievalRequest, interp_vec: list[vec_mod.VecHit],
    ) -> list[str]:
        return [a for a, _src in self._resolve_anchors_tagged(req, interp_vec)]

    def _resolve_anchors_tagged(
        self, req: RetrievalRequest, interp_vec: list[vec_mod.VecHit],
    ) -> list[tuple[str, str]]:
        """Caller anchors ∪ project pinned ∪ top-vec interp hits, tagged with
        the source of each anchor so verbose mode can show 'why this locus
        seeded the PPR pass'."""
        if req.anchors:
            return [(a, "caller") for a in req.anchors]
        pinned = self.projects_repo.members(req.project_id, roles=["pinned"])
        top_vec_anchors = [h.node_id for h in interp_vec[:5]]
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for a in pinned:
            if a not in seen:
                out.append((a, "pinned"))
                seen.add(a)
        for a in top_vec_anchors:
            if a not in seen:
                out.append((a, "top_vec_interp"))
                seen.add(a)
        return out

    def _walk_routes(
        self,
        *,
        interp_id: str,
        interp_score: float,
        routed_scores: dict[str, float],
        per_raw_trace: dict[str, list[RouteHop]],
    ) -> None:
        """From `interp_id`, walk cites and (optionally) derives_from·cites,
        accumulating route bonuses on raws and recording the trace path.

        Depth 1: direct cites. Depth 2: derives_from then cites — captures the
        case where the locus inherits routing from upstream loci. ROUTE_DEPTH
        controls the cap; ROUTE_DECAY downweights deeper hops.
        """
        # Depth 1: direct cites
        cites = self.edges_repo.from_node(interp_id, types=["cites"])
        for e in cites:
            bonus = interp_score * ROUTE_BONUS_GAIN
            routed_scores[e.dst] = routed_scores.get(e.dst, 0.0) + bonus
            hop = RouteHop(
                src=interp_id, dst=e.dst, edge_type="cites",
                interp_score=interp_score,
            )
            per_raw_trace.setdefault(e.dst, []).append(hop)

        if ROUTE_DEPTH < 2:
            return

        # Depth 2: walk derives_from forward, then cites
        upstream = self.edges_repo.from_node(interp_id, types=["derives_from"])
        for d in upstream:
            decayed = interp_score * ROUTE_DECAY
            up_cites = self.edges_repo.from_node(d.dst, types=["cites"])
            for e in up_cites:
                bonus = decayed * ROUTE_BONUS_GAIN
                routed_scores[e.dst] = routed_scores.get(e.dst, 0.0) + bonus
                # Record both hops so the trace shows the derivation path.
                per_raw_trace.setdefault(e.dst, []).extend([
                    RouteHop(src=interp_id, dst=d.dst, edge_type="derives_from",
                             interp_score=interp_score),
                    RouteHop(src=d.dst, dst=e.dst, edge_type="cites",
                             interp_score=decayed),
                ])

    def _fuse(
        self,
        channels: list[tuple[str, list[str]]],
        weights: dict[str, float],
    ) -> dict[str, float]:
        return self._fuse_with_breakdown(channels, weights)[0]

    def _fuse_with_breakdown(
        self,
        channels: list[tuple[str, list[str]]],
        weights: dict[str, float],
    ) -> tuple[dict[str, float], dict[str, dict[str, float]], dict[str, dict[str, int]]]:
        """Same fusion as `_fuse`, also returning per-node-per-channel rank
        and weighted contribution. The breakdown is what the verbose MCP
        flag surfaces as `channel_scores` / `channel_ranks` on each locus."""
        totals: dict[str, float] = {}
        per_channel_scores: dict[str, dict[str, float]] = {}
        per_channel_ranks: dict[str, dict[str, int]] = {}
        for channel, ranked_ids in channels:
            w = weights.get(channel, 1.0)
            for rank, nid in enumerate(ranked_ids, start=1):
                contrib = w / (RRF_K + rank)
                totals[nid] = totals.get(nid, 0.0) + contrib
                per_channel_scores.setdefault(nid, {})[channel] = contrib
                per_channel_ranks.setdefault(nid, {})[channel] = rank
        return totals, per_channel_scores, per_channel_ranks

    def _why(
        self, *, node, routed: bool, direct: bool, trace: list[RouteHop],
    ) -> str:
        parts: list[str] = []
        if direct:
            parts.append("matched the query directly")
        if routed and trace:
            n_loci = len({hop.src for hop in trace})
            parts.append(f"routed via {n_loci} locus(es) of thought")
        return "; ".join(parts) if parts else "in the project"

    def _materialise_routing_interps(
        self,
        interp_ids: list[str],
        score_map: dict[str, float],
        *,
        channel_scores_by_id: dict[str, dict[str, float]] | None = None,
        channel_ranks_by_id: dict[str, dict[str, int]] | None = None,
        anchor_source_by_id: dict[str, str] | None = None,
    ) -> list[RoutingInterp]:
        if not interp_ids:
            return []
        cs = channel_scores_by_id or {}
        cr = channel_ranks_by_id or {}
        anchors = anchor_source_by_id or {}
        placeholders = ",".join("?" * len(interp_ids))
        rows = self.conn.execute(
            f"""
            SELECT n.id, n.subkind, n.title,
                   i.relation_md, i.overlap_md, i.source_anchor_md, i.angle
            FROM nodes n
            JOIN interpretation_nodes i ON i.node_id = n.id
            WHERE n.id IN ({placeholders})
            """,
            tuple(interp_ids),
        ).fetchall()
        out = [
            RoutingInterp(
                node_id=r["id"], subkind=r["subkind"], title=r["title"],
                relation_md=r["relation_md"] or "",
                overlap_md=r["overlap_md"] or "",
                source_anchor_md=r["source_anchor_md"] or "",
                angle=r["angle"],
                score=score_map.get(r["id"], 0.0),
                channel_scores=cs.get(r["id"], {}),
                channel_ranks=cr.get(r["id"], {}),
                anchor_source=anchors.get(r["id"]),
            )
            for r in rows
        ]
        # Sort by score descending so the UI shows the strongest router first.
        out.sort(key=lambda x: -x.score)
        return out

    def _materialise_pruned_loci(
        self,
        *,
        top_interps: list[tuple[str, float]],
        below_top_k: list[tuple[str, float]],
        used_interp_ids: set[str],
        interp_channel_ranks: dict[str, dict[str, int]],
        max_below_k: int = 5,
        max_no_edges: int = 5,
    ) -> list[PrunedLocus]:
        """Top-K loci that routed zero raws → 'no_routing_edges'.
        Top scorers below K → 'below_top_k'. Capped — we only show the most
        relevant pruned candidates, since the long tail is noise."""
        no_edge_ids = [
            (nid, score) for nid, score in top_interps
            if nid not in used_interp_ids
        ][:max_no_edges]
        below_ids = below_top_k[:max_below_k]

        wanted = list(no_edge_ids) + list(below_ids)
        if not wanted:
            return []

        ids = [nid for nid, _ in wanted]
        placeholders = ",".join("?" * len(ids))
        rows = {
            r["id"]: r for r in self.conn.execute(
                f"""
                SELECT n.id, n.subkind, n.title
                FROM nodes n
                WHERE n.id IN ({placeholders})
                """,
                tuple(ids),
            ).fetchall()
        }

        out: list[PrunedLocus] = []
        for nid, score in no_edge_ids:
            r = rows.get(nid)
            if r is None:
                continue
            out.append(PrunedLocus(
                node_id=nid, subkind=r["subkind"], title=r["title"],
                score=score, reason="no_routing_edges",
                channel_ranks=interp_channel_ranks.get(nid, {}),
            ))
        for nid, score in below_ids:
            r = rows.get(nid)
            if r is None:
                continue
            out.append(PrunedLocus(
                node_id=nid, subkind=r["subkind"], title=r["title"],
                score=score, reason="below_top_k",
                channel_ranks=interp_channel_ranks.get(nid, {}),
            ))
        return out


def _kind_match(node, include: list) -> bool:
    return node.kind in include or node.subkind in include


def _snippet_fallback(body: str) -> str:
    one_line = " ".join(body.split())
    return one_line[:200] + ("…" if len(one_line) > 200 else "")
