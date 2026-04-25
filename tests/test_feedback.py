"""Citation-level feedback diffing."""

from __future__ import annotations

from loci.agent import diff_citations, emit_feedback_traces

HANDLES = {"C1": "01N1", "C2": "01N2", "C3": "01N3"}


def test_kept_when_context_preserved():
    orig = "Position lives in the projection [C1] and that explains rotary [C2]."
    edited = orig  # identical
    diffs = diff_citations(orig, edited, HANDLES)
    by_node = {d.node_id: d.kind for d in diffs}
    assert by_node["01N1"] == "cited_kept"
    assert by_node["01N2"] == "cited_kept"


def test_dropped_when_handle_removed():
    orig = "Position lives in the projection [C1] and rotary [C2] explains."
    edited = "Position lives in the projection [C1] and other story."   # C2 gone
    diffs = diff_citations(orig, edited, HANDLES)
    by_node = {d.node_id: d.kind for d in diffs}
    assert by_node["01N2"] == "cited_dropped"
    assert by_node["01N1"] == "cited_kept"


def test_replaced_when_context_rewritten():
    orig = "Rotary embeddings encode position via complex rotation [C1] which is elegant."
    edited = "I now think positions actually emerge from a different mechanism entirely [C1] and the prior framing was wrong."
    diffs = diff_citations(orig, edited, HANDLES)
    by_node = {d.node_id: d.kind for d in diffs}
    assert by_node["01N1"] == "cited_replaced"


def test_unknown_handle_is_skipped():
    orig = "Hello [C99]."
    edited = "Hello [C99]."
    diffs = diff_citations(orig, edited, HANDLES)  # C99 not in HANDLES
    assert diffs == []


def test_emit_traces_writes_rows(conn, project):
    """Real flow: write a Response, diff, then emit traces (FK satisfied)."""
    from loci.citations import CitationTracker, ResponseRecord
    n1, n2, n3 = (_make_node(conn, project), _make_node(conn, project), _make_node(conn, project))
    rec = ResponseRecord(
        project_id=project.id, session_id="s",
        request={"q": "x"}, output="A [C1] B [C2] C [C3].",
        cited_node_ids=[n1, n2, n3], client="test",
    )
    rid = CitationTracker(conn).write_response(rec)

    diffs = diff_citations(
        "A [C1] B [C2] C [C3].",
        "A [C1] B C.",   # C2 and C3 dropped, C1 stays
        {"C1": n1, "C2": n2, "C3": n3},
    )
    counts = emit_feedback_traces(conn, project.id, rid, diffs)
    assert counts["cited_kept"] == 1
    assert counts["cited_dropped"] == 2

    feedback_rows = conn.execute(
        "SELECT kind FROM traces WHERE project_id = ? AND kind LIKE 'cited_%'",
        (project.id,),
    ).fetchall()
    kinds = sorted(r["kind"] for r in feedback_rows)
    assert kinds == sorted(["cited_kept", "cited_dropped", "cited_dropped"])


_NODE_COUNTER = [0]


def _make_node(conn, project) -> str:
    """Create a dummy interpretation node and add to the project; return id."""
    from loci.graph import InterpretationNode, NodeRepository, ProjectRepository
    _NODE_COUNTER[0] += 1
    n = InterpretationNode(
        subkind="pattern", title=f"t-{_NODE_COUNTER[0]}",
        body="x", origin="user_explicit_create",
    )
    NodeRepository(conn).create_interpretation(n)
    ProjectRepository(conn).add_member(project.id, n.id, role="included")
    return n.id
