"""Tests for `loci event conversation` CLI command."""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_project_toml(directory: Path, slug: str) -> None:
    """Write a .loci/project.toml binding file in the given directory."""
    loci_dir = directory / ".loci"
    loci_dir.mkdir(parents=True, exist_ok=True)
    (loci_dir / "project.toml").write_text(
        f'slug = "{slug}"\ncreated_at = "2026-01-01"\n',
        encoding="utf-8",
    )


def _run_event_conversation(
    loci_dir_path: Path,
    project: object,
    payload: dict,
    tmp_path: Path,
    monkeypatch,
    *,
    capture_conversation: bool = True,
    role: str = "user",
    cwd: str | None = None,
) -> None:
    """Invoke the event_conversation CLI handler with mocked stdin and settings."""
    from loci.config import get_settings

    monkeypatch.setenv("LOCI_DATA_DIR", str(loci_dir_path))
    monkeypatch.setenv("LOCI_CAPTURE_CONVERSATION", "true" if capture_conversation else "false")
    get_settings.cache_clear()

    # Write the project.toml binding
    _write_project_toml(tmp_path, project.slug)

    # Monkey-patch stdin
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))

    # Call the CLI function directly
    from loci.ui.cli import event_conversation

    effective_cwd = cwd if cwd is not None else str(tmp_path)
    event_conversation(role=role, project=None, cwd=effective_cwd)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_inserts_conversation_event(loci_dir, conn, project, tmp_path, monkeypatch):
    """A valid payload with a bound cwd inserts a conversation_events row."""
    payload = {"prompt": "Tell me about methodology", "cwd": str(tmp_path)}
    _run_event_conversation(loci_dir, project, payload, tmp_path, monkeypatch)

    # Re-open the connection to check the written row (the CLI opens its own conn)
    from loci.db.connection import connect
    from loci.db import init_schema
    init_schema()
    check_conn = connect()

    try:
        row = check_conn.execute(
            "SELECT * FROM conversation_events WHERE project_id = ?",
            (project.id,),
        ).fetchone()
    finally:
        check_conn.close()

    assert row is not None, "expected a conversation_events row"
    assert row["project_id"] == project.id
    assert "methodology" in row["text"]
    assert row["role"] == "user"


def test_disabled_by_setting(loci_dir, conn, project, tmp_path, monkeypatch):
    """When capture_conversation=False, no conversation_events row is inserted."""
    payload = {"prompt": "some user text", "cwd": str(tmp_path)}
    _run_event_conversation(
        loci_dir, project, payload, tmp_path, monkeypatch,
        capture_conversation=False,
    )

    from loci.db.connection import connect
    from loci.db import init_schema
    init_schema()
    check_conn = connect()
    try:
        row = check_conn.execute(
            "SELECT * FROM conversation_events WHERE project_id = ?",
            (project.id,),
        ).fetchone()
    finally:
        check_conn.close()

    assert row is None, "expected no conversation_events row when capture is disabled"


def test_relevance_match_enqueues(loci_dir, conn, project, tmp_path, monkeypatch):
    """If text fuzzy-matches a project top aspect, infer_interpretation jobs are enqueued."""
    # Insert a resource with a known aspect
    node_id = "01DDDDDDDDDDDDDDDDDDDDAA00"
    content_hash = node_id[:16]
    conn.execute(
        "INSERT INTO nodes(id, kind, subkind, title, body) VALUES (?,?,?,?,?)",
        (node_id, "raw", "md", "Methodology Paper", "a paper about research methods"),
    )
    conn.execute(
        "INSERT INTO raw_nodes(id, content_hash, canonical_path, mime, size_bytes) "
        "VALUES (?,?,?,?,?)",
        (node_id, content_hash, "/tmp/method.md", "text/markdown", 30),
    )
    # Pin the resource to the project so top_aspects finds it
    from loci.graph.models import now_iso
    conn.execute(
        "INSERT INTO project_membership(project_id, node_id, role, added_at, added_by) "
        "VALUES (?,?,?,?,?)",
        (project.id, node_id, "included", now_iso(), "test"),
    )

    # Tag it with a project-scoped aspect
    from loci.graph.aspects import AspectRepository
    repo = AspectRepository(conn)
    repo.tag_resource(
        resource_id=node_id,
        aspect_labels=["methodology"],
        source="user",
        confidence=1.0,
        project_id=project.id,
    )

    # Lower the relevance cutoff so our short text matches more easily
    monkeypatch.setenv("LOCI_CONVERSATION_RELEVANCE_CUTOFF", "0.1")
    from loci.config import get_settings
    get_settings.cache_clear()

    payload = {
        "prompt": "I am studying methodology and research design in depth",
        "cwd": str(tmp_path),
    }
    _run_event_conversation(loci_dir, project, payload, tmp_path, monkeypatch)

    from loci.db.connection import connect
    from loci.db import init_schema
    init_schema()
    check_conn = connect()
    try:
        job_count = check_conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE kind = 'infer_interpretation' "
            "AND project_id = ?",
            (project.id,),
        ).fetchone()["n"]
    finally:
        check_conn.close()

    assert job_count >= 1, f"expected at least 1 infer_interpretation job, got {job_count}"
