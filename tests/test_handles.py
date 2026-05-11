"""Tests for loci.graph.handles — short-id generation and handle resolution."""

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


# ---------------------------------------------------------------------------
# 3.1 short_id tests
# ---------------------------------------------------------------------------


def test_short_id_stable():
    """Same resource_id always produces the same short_id."""
    from loci.graph.handles import short_id

    rid = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert short_id(rid) == short_id(rid)
    assert short_id(rid) == short_id(rid)  # deterministic across repeated calls


def test_short_id_format():
    """short_id starts with 'rid_' and is exactly 10 chars total."""
    from loci.graph.handles import short_id

    rid = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    sid = short_id(rid)
    assert sid.startswith("rid_"), f"expected 'rid_' prefix, got {sid!r}"
    assert len(sid) == 10, f"expected 10 chars, got {len(sid)}: {sid!r}"


def test_short_id_different_for_different_ids():
    """Different resource IDs produce different short_ids (no trivial collision for small sets)."""
    from loci.graph.handles import short_id

    ids = [
        "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "01ARZ3NDEKTSV4RRFFQ69G5FAW",
        "01ARZ3NDEKTSV4RRFFQ69G5FAX",
    ]
    sids = [short_id(r) for r in ids]
    assert len(set(sids)) == len(ids), "expected distinct short_ids for distinct resource IDs"


# ---------------------------------------------------------------------------
# 3.2 resolve_handle tests
# ---------------------------------------------------------------------------


def test_resolve_by_short_id(conn, project, workspace):
    """Inserting a resource and resolving its short_id returns the UUID."""
    from loci.graph.handles import resolve_handle, short_id

    rid = "01BBBBBBBBBBBBBBBBBBBBBBBB"
    _insert_raw_node(conn, rid, "Short ID Test")

    # Add to workspace so it's reachable via project_effective_members.
    conn.execute(
        "INSERT INTO workspace_membership(workspace_id, node_id) VALUES (?, ?)",
        (workspace.id, rid),
    )
    conn.commit()

    sid = short_id(rid)
    resolved = resolve_handle(sid, project.id, conn)
    assert resolved == rid, f"expected {rid!r}, got {resolved!r}"


def test_resolve_by_uuid(conn, project, workspace):
    """Passing the exact UUID resolves to that UUID."""
    from loci.graph.handles import resolve_handle

    rid = "01CCCCCCCCCCCCCCCCCCCCCCCC"
    _insert_raw_node(conn, rid, "UUID Test")
    conn.execute(
        "INSERT INTO workspace_membership(workspace_id, node_id) VALUES (?, ?)",
        (workspace.id, rid),
    )
    conn.commit()

    resolved = resolve_handle(rid, project.id, conn)
    assert resolved == rid


def test_resolve_by_fuzzy_title(conn, project, workspace):
    """Fuzzy title match returns the correct resource UUID."""
    from loci.graph.handles import resolve_handle

    rid = "01DDDDDDDDDDDDDDDDDDDDDDDD"
    _insert_raw_node(conn, rid, "Statistical Methods 2014")
    conn.execute(
        "INSERT INTO workspace_membership(workspace_id, node_id) VALUES (?, ?)",
        (workspace.id, rid),
    )
    conn.commit()

    resolved = resolve_handle("stat methods", project.id, conn)
    assert resolved == rid, f"expected {rid!r} from fuzzy match, got {resolved!r}"


def test_resolve_returns_none_for_unknown(conn, project):
    """Unknown handles return None rather than raising."""
    from loci.graph.handles import resolve_handle

    result = resolve_handle("rid_zzzzzz", project.id, conn)
    assert result is None
