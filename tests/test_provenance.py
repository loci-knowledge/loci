"""Tests for aspect provenance (aspect_provenance table + AspectRepository hooks)."""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_raw_node(conn, node_id: str, title: str = "Test Node") -> None:
    """Insert a minimal node + raw_node row."""
    content_hash = node_id[:16]
    conn.execute(
        "INSERT INTO nodes(id, kind, subkind, title, body) VALUES (?,?,?,?,?)",
        (node_id, "raw", "md", title, "body text"),
    )
    conn.execute(
        "INSERT INTO raw_nodes(id, content_hash, canonical_path, mime, size_bytes) "
        "VALUES (?,?,?,?,?)",
        (node_id, content_hash, f"/tmp/{node_id}.md", "text/markdown", 42),
    )


def _provenance_rows(conn, resource_id: str) -> list:
    """Return all aspect_provenance rows for a resource."""
    return conn.execute(
        "SELECT * FROM aspect_provenance WHERE resource_id = ? ORDER BY recorded_at",
        (resource_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_tag_resource_writes_provenance(conn):
    """tag_resource() appends a row to aspect_provenance with action='added'."""
    from loci.graph.aspects import AspectRepository

    rid = "01EEEEEEEEEEEEEEEEEEEEEEEE"
    _insert_raw_node(conn, rid)

    repo = AspectRepository(conn)
    repo.tag_resource(rid, ["reproducibility"], source="user", confidence=1.0)
    conn.commit()

    rows = _provenance_rows(conn, rid)
    assert len(rows) == 1, f"expected 1 provenance row, got {len(rows)}"
    row = rows[0]
    assert row["action"] == "added"
    assert row["source"] == "user"
    assert row["project_id"] is None
    assert abs((row["confidence"] or 0) - 1.0) < 1e-6


def test_untag_writes_removed_action(conn):
    """untag_resource() appends a row with action='removed'."""
    from loci.graph.aspects import AspectRepository

    rid = "01FFFFFFFFFFFFFFFFFFFFFFFF"
    _insert_raw_node(conn, rid)

    repo = AspectRepository(conn)
    repo.tag_resource(rid, ["p-hacking"], source="user", confidence=0.8)
    conn.commit()

    repo.untag_resource(rid, ["p-hacking"])
    conn.commit()

    rows = _provenance_rows(conn, rid)
    assert len(rows) == 2, f"expected 2 rows (add + remove), got {len(rows)}"
    actions = [r["action"] for r in rows]
    assert "added" in actions
    assert "removed" in actions


def test_provenance_append_only(conn):
    """Multiple tag/untag cycles produce multiple rows (append-only, not overwrite)."""
    from loci.graph.aspects import AspectRepository

    rid = "01GGGGGGGGGGGGGGGGGGGGGGGG"
    _insert_raw_node(conn, rid)

    repo = AspectRepository(conn)

    # Cycle 1: add
    repo.tag_resource(rid, ["statistics"], source="user", confidence=1.0)
    conn.commit()

    # Cycle 2: remove
    repo.untag_resource(rid, ["statistics"])
    conn.commit()

    # Cycle 3: add again with different confidence
    repo.tag_resource(rid, ["statistics"], source="inferred", confidence=0.6)
    conn.commit()

    rows = _provenance_rows(conn, rid)
    assert len(rows) == 3, (
        f"expected 3 provenance rows (2 added + 1 removed), got {len(rows)}: "
        + str([dict(r) for r in rows])
    )
    actions = [r["action"] for r in rows]
    assert actions.count("added") == 2
    assert actions.count("removed") == 1


def test_provenance_project_scoped(conn, project):
    """tag_resource with project_id stores the correct project_id in provenance."""
    from loci.graph.aspects import AspectRepository

    rid = "01HHHHHHHHHHHHHHHHHHHHHHHH"
    _insert_raw_node(conn, rid)

    repo = AspectRepository(conn)
    repo.tag_resource(rid, ["methodology"], source="llm", confidence=0.9, project_id=project.id)
    conn.commit()

    rows = _provenance_rows(conn, rid)
    assert len(rows) == 1
    assert rows[0]["project_id"] == project.id
    assert rows[0]["source"] == "llm"


def test_add_provenance_entry_helper(conn):
    """add_provenance_entry() writes a row directly."""
    from loci.graph.aspects import AspectRepository

    rid = "01IIIIIIIIIIIIIIIIIIIIIIII"
    _insert_raw_node(conn, rid)

    repo = AspectRepository(conn)
    # Ensure the aspect exists.
    aspect = repo.ensure_aspect("test-aspect", source="user")

    repo.add_provenance_entry(
        project_id=None,
        resource_id=rid,
        aspect_id=aspect.id,
        action="confirmed",
        source="user",
        confidence=1.0,
        rationale="Manual review confirmed relevance",
    )
    conn.commit()

    rows = _provenance_rows(conn, rid)
    assert len(rows) == 1
    assert rows[0]["action"] == "confirmed"
    assert rows[0]["rationale"] == "Manual review confirmed relevance"
