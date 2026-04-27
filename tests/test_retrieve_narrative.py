"""Tests for the verbose / narrative retrieval surface.

Three things to verify:

1. `trace_narrative` renders a per-raw markdown story that names the locus +
   edge type for routed raws, and flags direct hits inline.
2. The pipeline now populates `channel_ranks` / `channel_scores` on routing
   loci and tags PPR anchors with their `anchor_source`.
3. `pruned_loci` surfaces top-K loci that routed nothing — the verbose-mode
   "we considered this but it had no path to evidence" panel.
"""

from __future__ import annotations

from loci.graph import (
    EdgeRepository,
    InterpretationNode,
    NodeRepository,
    ProjectRepository,
)
from loci.ingest import scan_path
from loci.retrieve import (
    RetrievalRequest,
    RetrievedNode,
    Retriever,
    RoutingInterp,
    render_trace_narrative,
)


def test_render_trace_narrative_names_locus_for_cited_raw():
    """A raw routed via a single cites hop should get a 'cites ← Locus X' line."""
    locus_id = "01" + "L" * 24
    raw_id = "01" + "R" * 24
    nodes = [
        RetrievedNode(
            node_id=raw_id, kind="raw", subkind="md",
            title="Rotary Embeddings", snippet="...", score=0.84,
            why="routed via 1 locus",
        ),
    ]
    # Inject the trace hop manually — the renderer reads it off the node.
    from loci.retrieve.pipeline import RouteHop
    nodes[0].trace = [RouteHop(
        src=locus_id, dst=raw_id, edge_type="cites", interp_score=0.62,
    )]
    routing = [RoutingInterp(
        node_id=locus_id, subkind="decision", title="Position machinery",
        relation_md="...", overlap_md="...", source_anchor_md="...",
        angle=None, score=0.62,
    )]
    out = render_trace_narrative(nodes=nodes, routing_interps=routing)
    assert "Rotary Embeddings" in out
    assert "cites" in out
    assert "Position machinery" in out
    assert "0.62" in out  # routing score surfaces


def test_render_trace_narrative_flags_direct_hits():
    """A raw with no trace should be flagged as a direct hit so the user
    knows the interpretation layer did not contribute."""
    nodes = [RetrievedNode(
        node_id="01" + "R" * 24, kind="raw", subkind="md",
        title="Cross Attention", snippet="...", score=0.4,
        why="matched the query directly",
    )]
    out = render_trace_narrative(nodes=nodes, routing_interps=[])
    assert "Cross Attention" in out
    assert "direct hit" in out


def test_render_trace_narrative_handles_derives_from_chain():
    """A two-hop path (derives_from then cites) should produce two arrow lines."""
    upstream_id = "01" + "U" * 24
    downstream_id = "01" + "D" * 24
    raw_id = "01" + "R" * 24
    from loci.retrieve.pipeline import RouteHop
    nodes = [RetrievedNode(
        node_id=raw_id, kind="raw", subkind="md",
        title="The Raw", snippet="...", score=0.7, why="routed",
    )]
    nodes[0].trace = [
        RouteHop(src=downstream_id, dst=upstream_id, edge_type="derives_from",
                 interp_score=0.5),
        RouteHop(src=upstream_id, dst=raw_id, edge_type="cites",
                 interp_score=0.25),
    ]
    routing = [
        RoutingInterp(
            node_id=upstream_id, subkind="philosophy", title="Upstream",
            relation_md="", overlap_md="", source_anchor_md="",
            angle=None, score=0.5,
        ),
        RoutingInterp(
            node_id=downstream_id, subkind="decision", title="Downstream",
            relation_md="", overlap_md="", source_anchor_md="",
            angle=None, score=0.5,
        ),
    ]
    out = render_trace_narrative(nodes=nodes, routing_interps=routing)
    assert "derives_from" in out
    assert "cites" in out
    # Both locus titles should appear by name.
    assert "Upstream" in out
    assert "Downstream" in out


def test_routing_locus_carries_channel_breakdown(
    conn, fake_embedder, project, workspace, corpus_dir,
):
    """When retrieve runs, each surviving routing locus should carry the
    per-channel rank breakdown — verbose-mode UIs use this to explain why
    a locus scored high (e.g. 'lex rank 1, vec rank 12')."""
    scan_path(conn, workspace.id, corpus_dir, embedder=fake_embedder)
    raw_id = conn.execute(
        "SELECT id FROM nodes WHERE title='Rotary Embeddings'",
    ).fetchone()["id"]
    locus = InterpretationNode(
        subkind="decision", title="Rotary anchor",
        body="",
        relation_md="rotary embeddings encode position",
        overlap_md="rotary embeddings encode position",
        source_anchor_md="Section 3.2",
        origin="user_explicit_create",
    )
    NodeRepository(conn).create_interpretation(
        locus, embedding=fake_embedder.encode("rotary embeddings encode position"),
    )
    ProjectRepository(conn).add_member(project.id, locus.id, role="included")
    EdgeRepository(conn).create(locus.id, raw_id, type="cites")

    r = Retriever(conn, embedder=fake_embedder).retrieve(
        RetrievalRequest(
            project_id=project.id, query="rotary embeddings encode position",
            anchors=[locus.id], k=3,
        ),
    )
    routing_locus = next(
        (ri for ri in r.routing_interps if ri.node_id == locus.id), None,
    )
    assert routing_locus is not None
    # The locus should appear in at least one channel (lex/vec/ppr) — we
    # passed it as an anchor, so PPR will rank it.
    assert routing_locus.channel_ranks, "expected per-channel rank breakdown"
    assert any(
        ch in routing_locus.channel_ranks for ch in ("lex", "vec", "ppr")
    )
    # Caller-passed anchor → anchor_source should be 'caller'.
    assert routing_locus.anchor_source == "caller"


def test_pruned_loci_includes_top_locus_with_no_edges(
    conn, fake_embedder, project, workspace, corpus_dir,
):
    """A locus that scores well via lex/vec but has neither cites nor
    derives_from edges should land in pruned_loci with reason
    'no_routing_edges'. The user can see we considered it but it didn't
    point at any evidence."""
    scan_path(conn, workspace.id, corpus_dir, embedder=fake_embedder)
    orphan = InterpretationNode(
        subkind="philosophy", title="Orphaned thought",
        body="",
        # Strong textual overlap with the query — guarantees a good lex score.
        relation_md="rotary rotary rotary embeddings positions",
        overlap_md="rotary embeddings positions",
        source_anchor_md="(no source)",
        origin="user_explicit_create",
    )
    NodeRepository(conn).create_interpretation(
        orphan, embedding=fake_embedder.encode("rotary embeddings positions"),
    )
    ProjectRepository(conn).add_member(project.id, orphan.id, role="included")
    # No cites, no derives_from — this locus has no path to any raw.

    r = Retriever(conn, embedder=fake_embedder).retrieve(
        RetrievalRequest(
            project_id=project.id, query="rotary embeddings positions", k=3,
        ),
    )
    pruned_ids = {pl.node_id for pl in r.pruned_loci}
    assert orphan.id in pruned_ids
    pruned = next(pl for pl in r.pruned_loci if pl.node_id == orphan.id)
    assert pruned.reason == "no_routing_edges"


def test_retrieval_response_carries_trace_narrative(
    conn, fake_embedder, project, workspace, corpus_dir,
):
    """The pipeline pre-renders a trace_narrative on every response so both
    MCP and HTTP endpoints can ship it without recomputing."""
    scan_path(conn, workspace.id, corpus_dir, embedder=fake_embedder)
    r = Retriever(conn, embedder=fake_embedder).retrieve(
        RetrievalRequest(project_id=project.id, query="rotary", k=3),
    )
    assert isinstance(r.trace_narrative, str)
    assert r.trace_narrative  # non-empty when there are results
