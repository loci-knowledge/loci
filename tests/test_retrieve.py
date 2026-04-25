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


def test_anchored_ppr_boosts_anchor_neighbours(conn, fake_embedder, project, workspace, corpus_dir):
    _seed_basic(conn, fake_embedder, project, workspace, corpus_dir)
    nodes_repo = NodeRepository(conn)
    edges_repo = EdgeRepository(conn)
    # Two interps; one connects to the rotary raw, one is unrelated.
    iA = InterpretationNode(subkind="pattern", title="A pattern",
                              body="anchored interp body",
                              origin="user_explicit_create")
    iB = InterpretationNode(subkind="pattern", title="B pattern",
                              body="unrelated body",
                              origin="user_explicit_create")
    nodes_repo.create_interpretation(iA, embedding=fake_embedder.encode(iA.body))
    nodes_repo.create_interpretation(iB, embedding=fake_embedder.encode(iB.body))
    ProjectRepository(conn).add_member(project.id, iA.id, role="included")
    ProjectRepository(conn).add_member(project.id, iB.id, role="included")
    edges_repo.create(iA.id, iB.id, type="reinforces")

    r = Retriever(conn, embedder=fake_embedder).retrieve(
        RetrievalRequest(
            project_id=project.id, query="something",
            anchors=[iA.id], k=4,
        ),
    )
    ids = [n.node_id for n in r.nodes]
    # iA and iB both reachable from anchor; both should appear above pure
    # vec/lex hits that aren't connected.
    assert iA.id in ids
    assert iB.id in ids


def test_status_filter_excludes_dismissed(conn, fake_embedder, project, workspace, corpus_dir):
    _seed_basic(conn, fake_embedder, project, workspace, corpus_dir)
    # Mark one raw as dismissed.
    raw_id = conn.execute("SELECT id FROM nodes WHERE title='Cross Attention'").fetchone()["id"]
    NodeRepository(conn).set_status(raw_id, "dismissed")
    r = Retriever(conn, embedder=fake_embedder).retrieve(
        RetrievalRequest(project_id=project.id, query="cross attention", k=5),
    )
    assert raw_id not in {n.node_id for n in r.nodes}


def test_why_string_mentions_lex_terms(conn, fake_embedder, project, workspace, corpus_dir):
    _seed_basic(conn, fake_embedder, project, workspace, corpus_dir)
    r = Retriever(conn, embedder=fake_embedder).retrieve(
        RetrievalRequest(project_id=project.id, query="rotary embeddings", k=3),
    )
    rotary = next((n for n in r.nodes if n.title == "Rotary Embeddings"), None)
    assert rotary is not None
    assert "matched on" in rotary.why
