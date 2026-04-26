"""Graph CRUD invariants."""

from __future__ import annotations

import numpy as np
import pytest

from loci.graph import (
    EdgeRepository,
    InterpretationNode,
    NodeRepository,
    Project,
    ProjectRepository,
    RawNode,
)


def _vec():
    v = np.random.randn(384).astype(np.float32)
    return v / np.linalg.norm(v)


def test_create_and_round_trip_raw(conn):
    repo = NodeRepository(conn)
    node = RawNode(
        subkind="md", title="X", body="some body",
        content_hash="aaaaaaaaaaaaaaaa", canonical_path="/tmp/x.md",
        mime="text/markdown", size_bytes=10, tags=["t1", "t2"],
    )
    repo.create_raw(node, embedding=_vec())
    fetched = repo.get(node.id)
    assert fetched is not None
    assert fetched.kind == "raw"
    assert fetched.title == "X"
    assert sorted(fetched.tags) == ["t1", "t2"]


def test_dedup_by_content_hash(conn):
    repo = NodeRepository(conn)
    node = RawNode(
        subkind="md", title="X", body="b",
        content_hash="ffffffffffffffff", canonical_path="/tmp/x.md",
        mime="text/markdown", size_bytes=1,
    )
    repo.create_raw(node)
    found = repo.find_raw_by_hash("ffffffffffffffff")
    assert found is not None and found.id == node.id


def test_derives_from_is_directed(conn):
    """derives_from is a directed edge — no reciprocal is created."""
    repo_n = NodeRepository(conn)
    repo_e = EdgeRepository(conn)
    a = InterpretationNode(subkind="decision", title="A", body="a", origin="user_explicit_create")
    b = InterpretationNode(subkind="decision", title="B", body="b", origin="user_explicit_create")
    repo_n.create_interpretation(a)
    repo_n.create_interpretation(b)
    edge = repo_e.create(a.id, b.id, type="derives_from")
    assert edge.src == a.id and edge.dst == b.id
    rows = conn.execute(
        "SELECT src, dst FROM edges WHERE src IN (?,?) AND dst IN (?,?)",
        (a.id, b.id, a.id, b.id),
    ).fetchall()
    assert len(rows) == 1


def test_derives_from_cycle_rejected(conn):
    from loci.graph.edges import EdgeError
    repo_n = NodeRepository(conn)
    repo_e = EdgeRepository(conn)
    a = InterpretationNode(subkind="decision", title="A", body="a", origin="user_explicit_create")
    b = InterpretationNode(subkind="decision", title="B", body="b", origin="user_explicit_create")
    c = InterpretationNode(subkind="decision", title="C", body="c", origin="user_explicit_create")
    for n in (a, b, c):
        repo_n.create_interpretation(n)
    repo_e.create(a.id, b.id, type="derives_from")
    repo_e.create(b.id, c.id, type="derives_from")
    # Closing the cycle c → a must be rejected.
    with pytest.raises(EdgeError):
        repo_e.create(c.id, a.id, type="derives_from")


def test_cites_direction_enforced(conn):
    from loci.graph.edges import EdgeError
    repo_n = NodeRepository(conn)
    repo_e = EdgeRepository(conn)
    a = InterpretationNode(subkind="decision", title="A", body="a", origin="user_explicit_create")
    raw = RawNode(
        subkind="md", title="R", body="r",
        content_hash="cccccccccccccccc", canonical_path="/tmp/r.md",
        mime="text/markdown", size_bytes=1,
    )
    repo_n.create_interpretation(a)
    repo_n.create_raw(raw)
    # Valid direction: interp → raw.
    repo_e.create(a.id, raw.id, type="cites")
    # Invalid: raw → interp.
    with pytest.raises(EdgeError):
        repo_e.create(raw.id, a.id, type="cites")


def test_edge_create_idempotent(conn):
    repo_n = NodeRepository(conn)
    repo_e = EdgeRepository(conn)
    a = InterpretationNode(subkind="decision", title="A", body="a", origin="user_explicit_create")
    b = InterpretationNode(subkind="decision", title="B", body="b", origin="user_explicit_create")
    repo_n.create_interpretation(a)
    repo_n.create_interpretation(b)
    repo_e.create(a.id, b.id, type="derives_from")
    repo_e.create(a.id, b.id, type="derives_from")
    n_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    assert n_edges == 1


def test_dirty_propagates_one_hop(conn):
    repo_n = NodeRepository(conn)
    repo_e = EdgeRepository(conn)
    a = InterpretationNode(subkind="decision", title="A", body="a", origin="user_explicit_create")
    b = InterpretationNode(subkind="decision", title="B", body="b", origin="user_explicit_create")
    repo_n.create_interpretation(a)
    repo_n.create_interpretation(b)
    repo_e.create(a.id, b.id, type="derives_from")
    repo_n.update_body(a.id, body="updated body")
    b_status = conn.execute("SELECT status FROM nodes WHERE id=?", (b.id,)).fetchone()["status"]
    assert b_status == "dirty"


def test_project_membership_idempotent(conn):
    pr = ProjectRepository(conn)
    p = pr.create(Project(slug="p", name="P"))
    a = InterpretationNode(subkind="decision", title="A", body="a", origin="user_explicit_create")
    NodeRepository(conn).create_interpretation(a)
    pr.add_member(p.id, a.id, role="included")
    pr.add_member(p.id, a.id, role="pinned")  # upgrade role
    members = pr.members(p.id, roles=["pinned"])
    assert members == [a.id]


def test_status_transition_via_repo(conn):
    repo = NodeRepository(conn)
    a = InterpretationNode(subkind="decision", title="A", body="a", origin="user_explicit_create",
                            status="proposed")
    repo.create_interpretation(a)
    repo.set_status(a.id, "live")
    repo.bump_confidence(a.id, +0.15)
    row = conn.execute("SELECT status, confidence FROM nodes WHERE id=?", (a.id,)).fetchone()
    assert row["status"] == "live"
    assert row["confidence"] == pytest.approx(1.0)  # capped at 1.0


def test_bump_access(conn):
    repo = NodeRepository(conn)
    a = InterpretationNode(subkind="decision", title="A", body="a", origin="user_explicit_create")
    repo.create_interpretation(a)
    repo.bump_access(a.id)
    repo.bump_access(a.id)
    row = conn.execute("SELECT access_count, last_accessed_at FROM nodes WHERE id=?", (a.id,)).fetchone()
    assert row["access_count"] == 2
    assert row["last_accessed_at"] is not None
