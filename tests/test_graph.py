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


def test_symmetric_edge_creates_reciprocal(conn):
    repo_n = NodeRepository(conn)
    repo_e = EdgeRepository(conn)
    a = InterpretationNode(subkind="pattern", title="A", body="a", origin="user_explicit_create")
    b = InterpretationNode(subkind="pattern", title="B", body="b", origin="user_explicit_create")
    repo_n.create_interpretation(a)
    repo_n.create_interpretation(b)
    edges = repo_e.create(a.id, b.id, type="reinforces")
    assert len(edges) == 2
    src_dst = {(e.src, e.dst) for e in edges}
    assert (a.id, b.id) in src_dst and (b.id, a.id) in src_dst


def test_specializes_creates_inverse_generalizes(conn):
    repo_n = NodeRepository(conn)
    repo_e = EdgeRepository(conn)
    a = InterpretationNode(subkind="pattern", title="A", body="a", origin="user_explicit_create")
    b = InterpretationNode(subkind="pattern", title="B", body="b", origin="user_explicit_create")
    repo_n.create_interpretation(a)
    repo_n.create_interpretation(b)
    repo_e.create(a.id, b.id, type="specializes")
    types = sorted(r["type"] for r in conn.execute(
        "SELECT type FROM edges WHERE src IN (?,?) AND dst IN (?,?)",
        (a.id, b.id, a.id, b.id),
    ).fetchall())
    assert types == ["generalizes", "specializes"]


def test_edge_create_idempotent(conn):
    repo_n = NodeRepository(conn)
    repo_e = EdgeRepository(conn)
    a = InterpretationNode(subkind="pattern", title="A", body="a", origin="user_explicit_create")
    b = InterpretationNode(subkind="pattern", title="B", body="b", origin="user_explicit_create")
    repo_n.create_interpretation(a)
    repo_n.create_interpretation(b)
    repo_e.create(a.id, b.id, type="reinforces")
    repo_e.create(a.id, b.id, type="reinforces")
    n_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    assert n_edges == 2  # 1 forward + 1 reciprocal, no duplicates


def test_dirty_propagates_one_hop(conn):
    repo_n = NodeRepository(conn)
    repo_e = EdgeRepository(conn)
    a = InterpretationNode(subkind="pattern", title="A", body="a", origin="user_explicit_create")
    b = InterpretationNode(subkind="pattern", title="B", body="b", origin="user_explicit_create")
    repo_n.create_interpretation(a)
    repo_n.create_interpretation(b)
    repo_e.create(a.id, b.id, type="extends")
    repo_n.update_body(a.id, body="updated body")
    b_status = conn.execute("SELECT status FROM nodes WHERE id=?", (b.id,)).fetchone()["status"]
    assert b_status == "dirty"


def test_project_membership_idempotent(conn):
    pr = ProjectRepository(conn)
    p = pr.create(Project(slug="p", name="P"))
    a = InterpretationNode(subkind="pattern", title="A", body="a", origin="user_explicit_create")
    NodeRepository(conn).create_interpretation(a)
    pr.add_member(p.id, a.id, role="included")
    pr.add_member(p.id, a.id, role="pinned")  # upgrade role
    members = pr.members(p.id, roles=["pinned"])
    assert members == [a.id]


def test_status_transition_via_repo(conn):
    repo = NodeRepository(conn)
    a = InterpretationNode(subkind="pattern", title="A", body="a", origin="user_explicit_create",
                            status="proposed")
    repo.create_interpretation(a)
    repo.set_status(a.id, "live")
    repo.bump_confidence(a.id, +0.15)
    row = conn.execute("SELECT status, confidence FROM nodes WHERE id=?", (a.id,)).fetchone()
    assert row["status"] == "live"
    assert row["confidence"] == pytest.approx(1.0)  # capped at 1.0


def test_bump_access(conn):
    repo = NodeRepository(conn)
    a = InterpretationNode(subkind="pattern", title="A", body="a", origin="user_explicit_create")
    repo.create_interpretation(a)
    repo.bump_access(a.id)
    repo.bump_access(a.id)
    row = conn.execute("SELECT access_count, last_accessed_at FROM nodes WHERE id=?", (a.id,)).fetchone()
    assert row["access_count"] == 2
    assert row["last_accessed_at"] is not None
