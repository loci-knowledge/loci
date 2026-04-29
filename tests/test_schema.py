"""Schema bring-up tests."""

from __future__ import annotations


def test_init_schema_creates_all_tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {r["name"] for r in rows}
    expected = {
        "nodes", "raw_nodes", "raw_chunks",
        "node_tags", "projects", "project_membership",
        "information_workspaces", "workspace_sources", "workspace_membership",
        "project_workspaces", "jobs",
        "aspect_vocab", "resource_aspects", "concept_edges",
        "resource_provenance", "resource_usage_log",
        "nodes_fts", "chunks_fts",
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


def test_init_schema_idempotent(loci_dir):
    """Running init_schema twice is a clean no-op (no errors, no duplicate tables)."""
    from loci.db import init_schema
    init_schema()
    init_schema()
