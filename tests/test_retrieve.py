"""Retrieval pipeline tests."""

from __future__ import annotations

from loci.graph import (
    EdgeRepository,
    InterpretationNode,
    NodeRepository,
    Project,
    ProjectRepository,
)
from loci.graph.models import Workspace
from loci.graph.workspaces import WorkspaceRepository
from loci.ingest import scan_path
from loci.retrieve import RetrievalRequest, Retriever


def _seed_basic(conn, embedder, project, workspace, corpus_dir):
    scan_path(conn, workspace.id, corpus_dir, embedder=embedder)


def test_lex_retrieval_returns_matching_doc(conn, fake_embedder, project, workspace, corpus_dir):
    _seed_basic(conn, fake_embedder, project, workspace, corpus_dir)
    r = Retriever(conn, embedder=fake_embedder).retrieve(
        RetrievalRequest(project_id=project.id, query="rotary", k=3),
    )
    titles = [n.title for n in r.nodes]
    assert "Rotary Embeddings" in titles


def test_retrieval_excludes_other_projects(conn, fake_embedder, corpus_dir):
    pr = ProjectRepository(conn)
    ws_repo = WorkspaceRepository(conn)
    p1 = pr.create(Project(slug="p1", name="P1"))
    p2 = pr.create(Project(slug="p2", name="P2"))
    ws1 = Workspace(slug="ws-p1", name="P1 workspace", kind="mixed")
    ws_repo.create(ws1)
    ws_repo.link_project(p1.id, ws1.id, role="primary")
    scan_path(conn, ws1.id, corpus_dir, embedder=fake_embedder)
    # p2 has no linked workspace — retrieve should return empty
    r = Retriever(conn, embedder=fake_embedder).retrieve(
        RetrievalRequest(project_id=p2.id, query="rotary", k=3),
    )
    assert r.nodes == []


def test_routing_interps_surface_in_response(conn, fake_embedder, project, workspace, corpus_dir):
    """A locus that derives from an anchor and cites a raw routes that raw to
    the user, and the locus appears in the routing_interps side panel."""
    _seed_basic(conn, fake_embedder, project, workspace, corpus_dir)
    nodes_repo = NodeRepository(conn)
    edges_repo = EdgeRepository(conn)

    raw_id = conn.execute(
        "SELECT id FROM nodes WHERE title='Rotary Embeddings'"
    ).fetchone()["id"]

    iA = InterpretationNode(
        subkind="decision", title="A pattern",
        body="",
        relation_md="Rotary embeddings supply the position machinery for this project's encoder.",
        overlap_md="Both projects rotate the residual stream to encode position.",
        source_anchor_md="Section 3.2 of the paper, equation (4).",
        origin="user_explicit_create",
    )
    iB = InterpretationNode(
        subkind="decision", title="B derived",
        body="",
        relation_md="B builds on the rotary insight from A.",
        overlap_md="Same rotation idea applied at attention input.",
        source_anchor_md="Section 3.3 — derived corollary.",
        origin="user_explicit_create",
    )
    nodes_repo.create_interpretation(iA, embedding=fake_embedder.encode("rotary position"))
    nodes_repo.create_interpretation(iB, embedding=fake_embedder.encode("derived rotation"))
    ProjectRepository(conn).add_member(project.id, iA.id, role="included")
    ProjectRepository(conn).add_member(project.id, iB.id, role="included")
    edges_repo.create(iA.id, raw_id, type="cites")
    edges_repo.create(iB.id, iA.id, type="derives_from")

    r = Retriever(conn, embedder=fake_embedder).retrieve(
        RetrievalRequest(
            project_id=project.id, query="rotary embeddings position",
            anchors=[iA.id], k=4,
        ),
    )
    # Result nodes are raws (default include).
    raw_ids = [n.node_id for n in r.nodes]
    assert raw_id in raw_ids
    # The locus that routed retrieval shows up in the side panel.
    routing_ids = [ri.node_id for ri in r.routing_interps]
    assert iA.id in routing_ids
    # The raw's trace records the routing hop.
    raw_node = next(n for n in r.nodes if n.node_id == raw_id)
    assert any(hop.src == iA.id and hop.edge_type == "cites" for hop in raw_node.trace)


def test_status_filter_excludes_dismissed(conn, fake_embedder, project, workspace, corpus_dir):
    _seed_basic(conn, fake_embedder, project, workspace, corpus_dir)
    # Mark one raw as dismissed.
    raw_id = conn.execute("SELECT id FROM nodes WHERE title='Cross Attention'").fetchone()["id"]
    NodeRepository(conn).set_status(raw_id, "dismissed")
    r = Retriever(conn, embedder=fake_embedder).retrieve(
        RetrievalRequest(project_id=project.id, query="cross attention", k=5),
    )
    assert raw_id not in {n.node_id for n in r.nodes}


def test_why_string_marks_direct_match(conn, fake_embedder, project, workspace, corpus_dir):
    _seed_basic(conn, fake_embedder, project, workspace, corpus_dir)
    r = Retriever(conn, embedder=fake_embedder).retrieve(
        RetrievalRequest(project_id=project.id, query="rotary embeddings", k=3),
    )
    rotary = next((n for n in r.nodes if n.title == "Rotary Embeddings"), None)
    assert rotary is not None
    # No interp routes for this raw, so the only route is the direct match.
    assert "matched the query directly" in rotary.why
