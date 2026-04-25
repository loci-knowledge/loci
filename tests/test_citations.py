"""Citation tracker tests."""

from __future__ import annotations

from loci.citations import CitationTracker, ResponseRecord
from loci.graph import InterpretationNode, NodeRepository, ProjectRepository


def _make_interp(conn, project, embedder):
    n = InterpretationNode(subkind="pattern", title="X", body="x body",
                              origin="user_explicit_create")
    NodeRepository(conn).create_interpretation(n, embedding=embedder.encode("x body"))
    ProjectRepository(conn).add_member(project.id, n.id, role="included")
    return n


def test_write_response_persists_citations(conn, fake_embedder, project):
    n = _make_interp(conn, project, fake_embedder)
    ct = CitationTracker(conn)
    rec = ResponseRecord(
        project_id=project.id, session_id="s", request={"q": "x"},
        output="hello", cited_node_ids=[n.id], client="test",
    )
    rid = ct.write_response(rec)
    fetched = ct.get_response(rid)
    assert fetched is not None
    assert fetched["output"] == "hello"
    assert fetched["cited_node_ids"] == [n.id]


def test_traces_emit_for_cited_and_retrieved(conn, fake_embedder, project):
    n1 = _make_interp(conn, project, fake_embedder)
    n2 = _make_interp(conn, project, fake_embedder)
    ct = CitationTracker(conn)
    rec = ResponseRecord(
        project_id=project.id, session_id="s", request={},
        output="o", cited_node_ids=[n1.id], client="t",
    )
    ct.write_response(rec, retrieved_node_ids=[n1.id, n2.id])
    cited = conn.execute("SELECT COUNT(*) FROM traces WHERE kind='cited'").fetchone()[0]
    retrieved = conn.execute("SELECT COUNT(*) FROM traces WHERE kind='retrieved'").fetchone()[0]
    assert cited == 1  # n1 was cited
    assert retrieved == 1  # n2 was retrieved-only
