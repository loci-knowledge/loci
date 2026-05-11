"""Tests for the record_event helper in loci.mcp.events."""

from __future__ import annotations

import asyncio


def _run(coro):
    """Run a coroutine synchronously in a fresh event loop."""
    return asyncio.run(coro)


def _insert_raw_node(conn, node_id: str) -> None:
    """Insert a minimal nodes + raw_nodes row."""
    content_hash = node_id[:16]
    conn.execute(
        "INSERT INTO nodes(id, kind, subkind, title, body) VALUES (?,?,?,?,?)",
        (node_id, "raw", "md", "Test", "body"),
    )
    conn.execute(
        "INSERT INTO raw_nodes(id, content_hash, canonical_path, mime, size_bytes) "
        "VALUES (?,?,?,?,?)",
        (node_id, content_hash, f"/tmp/{node_id}.md", "text/markdown", 4),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_record_event_inserts_log(conn, project):
    """record_event inserts a row into resource_usage_log with correct fields."""
    from loci.mcp.events import record_event

    rid = "01CCCCCCCCCCCCCCCCCCCCAA00"
    _insert_raw_node(conn, rid)

    session = "abc123session"
    _run(record_event(
        conn=conn,
        tool="loci_recall",
        project_id=project.id,
        resource_id=rid,
        query="semantic memory",
        session_hash=session,
        enqueue_inference=False,
    ))

    row = conn.execute(
        "SELECT * FROM resource_usage_log WHERE resource_id = ?",
        (rid,),
    ).fetchone()

    assert row is not None, "expected a resource_usage_log row"
    assert row["project_id"] == project.id
    assert row["query"] == "semantic memory"
    assert row["session_hash"] == session
    assert row["tool_call_type"] == "loci_recall"


def test_enqueues_inference_job(conn, project):
    """record_event with enqueue_inference=True creates an infer_interpretation job."""
    from loci.mcp.events import record_event

    rid = "01CCCCCCCCCCCCCCCCCCCCAA01"
    _insert_raw_node(conn, rid)

    _run(record_event(
        conn=conn,
        tool="loci_recall",
        project_id=project.id,
        resource_id=rid,
        session_hash=None,
        enqueue_inference=True,
        immediate=True,  # bypass daily bucket so fingerprint=None → always enqueues
    ))

    job_row = conn.execute(
        "SELECT * FROM jobs WHERE kind = 'infer_interpretation' AND project_id = ?",
        (project.id,),
    ).fetchone()

    assert job_row is not None, "expected an infer_interpretation job to be enqueued"
    import json
    payload = json.loads(job_row["payload"])
    assert payload["resource_id"] == rid


def test_bucket_deduplication(conn, project):
    """Calling record_event 5× same day only enqueues 1 infer_interpretation job."""
    from loci.mcp.events import record_event

    rid = "01CCCCCCCCCCCCCCCCCCCCAA02"
    _insert_raw_node(conn, rid)

    # Call 5 times with the same daily bucket (default behaviour, not immediate)
    for _ in range(5):
        _run(record_event(
            conn=conn,
            tool="loci_recall",
            project_id=project.id,
            resource_id=rid,
            session_hash=None,
            enqueue_inference=True,
            immediate=False,
        ))

    job_count = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE kind = 'infer_interpretation' AND project_id = ?",
        (project.id,),
    ).fetchone()["n"]

    assert job_count == 1, f"expected 1 job due to fingerprint dedup, got {job_count}"
