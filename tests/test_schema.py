"""Schema + migration tests."""

from __future__ import annotations


def test_migrate_creates_all_tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {r["name"] for r in rows}
    expected = {
        "_migrations", "nodes", "raw_nodes", "interpretation_nodes",
        "node_tags", "edges", "projects", "project_membership",
        "responses", "traces", "proposals", "jobs", "communities",
        "nodes_fts",  # FTS5 also creates internal `nodes_fts_*` shadow tables
    }
    missing = expected - names
    assert not missing, f"missing tables: {missing}"


def test_fts_trigger_on_insert(conn):
    conn.execute(
        "INSERT INTO nodes(id, kind, subkind, title, body) VALUES (?,?,?,?,?)",
        ("01AAAAAAAAAAAAAAAAAAAAAAAA", "raw", "md", "RoFormer", "rotary embeddings"),
    )
    hits = conn.execute(
        "SELECT node_id FROM nodes_fts WHERE nodes_fts MATCH ?", ("rotary",),
    ).fetchall()
    assert hits and hits[0]["node_id"] == "01AAAAAAAAAAAAAAAAAAAAAAAA"


def test_fts_trigger_on_tag_change(conn):
    conn.execute(
        "INSERT INTO nodes(id, kind, subkind, title, body) VALUES (?,?,?,?,?)",
        ("01BBBBBBBBBBBBBBBBBBBBBBBB", "raw", "md", "T", "no special words"),
    )
    conn.execute(
        "INSERT INTO node_tags(node_id, tag) VALUES (?,?)",
        ("01BBBBBBBBBBBBBBBBBBBBBBBB", "rocketry"),
    )
    hits = conn.execute(
        "SELECT node_id FROM nodes_fts WHERE nodes_fts MATCH ?", ("rocketry",),
    ).fetchall()
    assert hits and hits[0]["node_id"] == "01BBBBBBBBBBBBBBBBBBBBBBBB"


def test_vec_table_round_trip(conn):
    import struct
    blob = struct.pack("384f", *([0.5] * 384))
    conn.execute(
        "INSERT INTO nodes(id, kind, subkind, title, body) VALUES (?,?,?,?,?)",
        ("01CCCCCCCCCCCCCCCCCCCCCCCC", "raw", "md", "T", "x"),
    )
    conn.execute(
        "INSERT INTO node_vec(node_id, embedding) VALUES (?, ?)",
        ("01CCCCCCCCCCCCCCCCCCCCCCCC", blob),
    )
    hits = conn.execute(
        "SELECT node_id, distance FROM node_vec WHERE embedding MATCH ? AND k = 5",
        (blob,),
    ).fetchall()
    assert hits[0]["node_id"] == "01CCCCCCCCCCCCCCCCCCCCCCCC"
    assert hits[0]["distance"] == 0.0


def test_check_constraint_status_enum(conn):
    import sqlite3

    import pytest as pt
    with pt.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO nodes(id, kind, subkind, title, body, status) VALUES (?,?,?,?,?,?)",
            ("01DDDDDDDDDDDDDDDDDDDDDDDD", "raw", "md", "T", "x", "bogus_status"),
        )


def test_idempotent_migrate(loci_dir):
    """Running migrate twice should be a no-op the second time."""
    from loci.db import migrate
    first = migrate()
    second = migrate()
    assert first  # at least 0001 applied
    assert second == []  # nothing to apply
