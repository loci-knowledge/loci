"""Retrieval orchestrator.

Takes a `RetrievalRequest` (query + options) and returns a ranked list of
`RetrievedNode`s, plus the citations block PLAN.md guarantees on every
response.

Score fusion: we use Reciprocal Rank Fusion (RRF; Cormack & Clarke 2009).
Each channel (lex, vec, HyDE, PPR) produces a ranked list. RRF score for a
node is `Σ 1 / (k_rrf + rank_in_channel)`, summed across channels where it
appears. RRF is parameter-light (k_rrf=60 is the canonical default), robust
to scale differences between channels, and has held up well across IR papers.

Anchors: when the caller supplies anchors, PPR uses them. Otherwise we
fall back to the project's `pinned` members ∪ top-`k_anchors_from_vec` vec
hits — exactly the policy in PLAN.md §Retrieval ("falls back to the project's
pinned nodes plus the top-k vector hits as anchors").

`why` strings are derived heuristically from the channels that surfaced the
node — no extra LLM call. Format: `"<channel-fact>; <traversal-fact>"`.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from loci.embed.local import Embedder, get_embedder
from loci.graph.edges import EdgeRepository
from loci.graph.models import NodeKind, Subkind
from loci.graph.nodes import NodeRepository
from loci.graph.projects import ProjectRepository
from loci.retrieve import hyde as hyde_mod
from loci.retrieve import lex as lex_mod
from loci.retrieve import ppr as ppr_mod
from loci.retrieve import vec as vec_mod

log = logging.getLogger(__name__)

# Reciprocal-Rank-Fusion smoothing constant. The IR literature settled on 60
# as a robust default; we expose it as a constant in case future tuning needs it.
RRF_K = 60

# Default channel weights. These multiply the RRF contribution per channel.
# vec is weighted highest because PLAN's primary use case is semantic recall;
# lex catches the long tail of exact-term queries; HyDE adds a small boost
# only when explicitly requested; PPR brings in graph-aware reranking.
DEFAULT_WEIGHTS: dict[str, float] = {
    "lex": 1.0,
    "vec": 1.5,
    "hyde": 1.0,
    "ppr": 0.7,
}


@dataclass
class RetrievalRequest:
    project_id: str
    query: str
    k: int = 10
    anchors: list[str] = field(default_factory=list)
    include: list[NodeKind | Subkind] | None = None  # filter by kind/subkind
    hyde: bool = False
    # Channel widths — how many hits we pull per channel before fusion.
    k_lex: int = 30
    k_vec: int = 30
    k_hyde: int = 20
    # Anchor backfill from vec hits when caller didn't supply anchors.
    k_anchors_from_vec: int = 5


@dataclass
class RetrievedNode:
    node_id: str
    kind: NodeKind
    subkind: Subkind
    title: str
    snippet: str
    score: float
    why: str
    # Channels that surfaced this node and the rank in each (for explainability).
    channel_ranks: dict[str, int] = field(default_factory=dict)


@dataclass
class CitationEntry:
    node_id: str
    contributing_score: float
    edges_traversed: list[str]  # edge ids (or labels) that mattered for PPR


@dataclass
class RetrievalResponse:
    nodes: list[RetrievedNode]
    citations: list[CitationEntry]


class Retriever:
    """Stateful only by virtue of holding repos; cheap to construct per call."""

    def __init__(self, conn: sqlite3.Connection, embedder: Embedder | None = None) -> None:
        self.conn = conn
        self.embedder = embedder or get_embedder()
        self.nodes_repo = NodeRepository(conn)
        self.edges_repo = EdgeRepository(conn)
        self.projects_repo = ProjectRepository(conn)

    def retrieve(self, req: RetrievalRequest) -> RetrievalResponse:
        # 1. Lexical channel
        lex_hits = lex_mod.search(self.conn, req.project_id, req.query, k=req.k_lex)
        # 2. Vector channel
        vec_hits = vec_mod.search_text(
            self.conn, req.project_id, req.query, k=req.k_vec, embedder=self.embedder,
        )
        # 3. HyDE channel (optional)
        hyde_hits: list[vec_mod.VecHit] = []
        hypothetical = ""
        if req.hyde:
            hypothetical = hyde_mod.hypothesize(req.query)
            if hypothetical and hypothetical != req.query:
                hyde_hits = vec_mod.search_text(
                    self.conn, req.project_id, hypothetical,
                    k=req.k_hyde, embedder=self.embedder,
                )

        # 4. Anchor selection
        anchors = list(req.anchors)
        if not anchors:
            pinned = self.projects_repo.members(req.project_id, roles=["pinned"])
            top_vec_anchors = [
                h.node_id for h in vec_hits[: req.k_anchors_from_vec]
            ]
            # Combine + dedup, preserving order.
            seen: set[str] = set()
            for a in pinned + top_vec_anchors:
                if a not in seen:
                    anchors.append(a)
                    seen.add(a)

        # 5. PPR over the interpretation graph
        ppr_result = ppr_mod.run(self.conn, req.project_id, anchors)
        ppr_ranked = sorted(ppr_result.scores.items(), key=lambda kv: -kv[1])

        # 6. RRF fusion
        scores, channel_ranks = self._fuse(
            lex_hits=lex_hits,
            vec_hits=vec_hits,
            hyde_hits=hyde_hits,
            ppr_ranked=ppr_ranked,
        )

        # 7. Filter + materialise
        ranked_ids = sorted(scores.items(), key=lambda kv: -kv[1])
        materialised: list[RetrievedNode] = []
        snippet_by_id = {h.node_id: h.snippet for h in lex_hits}
        nodes_by_id = {n.id: n for n in self.nodes_repo.get_many(
            [nid for nid, _ in ranked_ids[: req.k * 4]]
        )}
        for nid, sc in ranked_ids:
            n = nodes_by_id.get(nid)
            if n is None:
                continue
            if req.include and not _kind_match(n, req.include):
                continue
            ranks = channel_ranks.get(nid, {})
            why = self._why(
                node=n, ranks=ranks, lex_terms=lex_mod._terms(req.query),
                ppr_anchors=ppr_result.anchors_used,
            )
            materialised.append(
                RetrievedNode(
                    node_id=nid, kind=n.kind, subkind=n.subkind, title=n.title,
                    snippet=snippet_by_id.get(nid, _snippet_fallback(n.body)),
                    score=sc, why=why, channel_ranks=ranks,
                )
            )
            # Update access stats.
            self.nodes_repo.bump_access(nid)
            if len(materialised) >= req.k:
                break

        # 8. Citation block. For each retrieved interp node, surface raw nodes
        # it `cites` so the caller has the full provenance fan-out.
        citations = self._citations(materialised)
        return RetrievalResponse(nodes=materialised, citations=citations)

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    @staticmethod
    def _fuse(
        *,
        lex_hits: list[lex_mod.LexHit],
        vec_hits: list[vec_mod.VecHit],
        hyde_hits: list[vec_mod.VecHit],
        ppr_ranked: list[tuple[str, float]],
    ) -> tuple[dict[str, float], dict[str, dict[str, int]]]:
        scores: dict[str, float] = {}
        channel_ranks: dict[str, dict[str, int]] = {}

        def fold(channel: str, ranked_ids: list[str]) -> None:
            w = DEFAULT_WEIGHTS.get(channel, 1.0)
            for rank, nid in enumerate(ranked_ids, start=1):
                contrib = w / (RRF_K + rank)
                scores[nid] = scores.get(nid, 0.0) + contrib
                channel_ranks.setdefault(nid, {})[channel] = rank

        fold("lex", [h.node_id for h in lex_hits])
        fold("vec", [h.node_id for h in vec_hits])
        fold("hyde", [h.node_id for h in hyde_hits])
        fold("ppr", [nid for nid, _ in ppr_ranked])
        return scores, channel_ranks

    @staticmethod
    def _why(*, node, ranks: dict[str, int], lex_terms: list[str],
             ppr_anchors: list[str]) -> str:
        """A short human-readable string explaining why this node showed up."""
        parts: list[str] = []
        if "lex" in ranks and lex_terms:
            # Find any term present in title/body — cheap, accurate enough.
            blob = f"{node.title} {node.body}".lower()
            matched = [t for t in lex_terms if t in blob][:3]
            if matched:
                parts.append(f"matched on {', '.join(matched)}")
        if "vec" in ranks:
            parts.append(f"semantically near query (rank {ranks['vec']})")
        if "hyde" in ranks:
            parts.append(f"matched the hypothetical answer (rank {ranks['hyde']})")
        if "ppr" in ranks and ppr_anchors:
            parts.append(f"reachable from anchors via the graph (rank {ranks['ppr']})")
        return "; ".join(parts) if parts else "in the project"

    def _citations(self, nodes: list[RetrievedNode]) -> list[CitationEntry]:
        """For each interp node retrieved, list its raw `cites` neighbours."""
        out: list[CitationEntry] = []
        for n in nodes:
            edges_traversed: list[str] = []
            if n.kind == "interpretation":
                cites = self.edges_repo.from_node(n.node_id, types=["cites"])
                edges_traversed = [f"cites→{e.dst}" for e in cites]
            out.append(CitationEntry(
                node_id=n.node_id, contributing_score=n.score,
                edges_traversed=edges_traversed,
            ))
        return out


def _kind_match(node, include: list) -> bool:
    return node.kind in include or node.subkind in include


def _snippet_fallback(body: str) -> str:
    """When FTS5 didn't surface this hit (vec-only / PPR-only), make a snippet
    from the head of the body — first 200 chars, single-line."""
    one_line = " ".join(body.split())
    return one_line[:200] + ("…" if len(one_line) > 200 else "")
