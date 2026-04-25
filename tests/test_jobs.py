"""Job queue + absorb pipeline tests."""

from __future__ import annotations

from loci.ingest import scan_path
from loci.jobs import enqueue
from loci.jobs.queue import claim_one, get_job
from loci.jobs.worker import run_once


def test_enqueue_and_claim(conn, project):
    jid = enqueue(conn, kind="absorb", project_id=project.id, payload={"k": 1})
    job = claim_one(conn)
    assert job["id"] == jid
    assert job["kind"] == "absorb"
    assert job["payload"] == {"k": 1}
    # Second claim returns None (queue empty)
    assert claim_one(conn) is None


def test_run_once_executes_absorb(conn, fake_embedder, project, corpus_dir):
    scan_path(conn, project.id, corpus_dir, embedder=fake_embedder)
    jid = enqueue(conn, kind="absorb", project_id=project.id)
    assert run_once(conn) is True
    job = get_job(conn, jid)
    assert job["status"] == "done"
    steps = job["result"]["steps"]
    assert "fs_audit" in steps
    assert "orphans" in steps
    assert "aliases" in steps


def test_unknown_kind_rejected_by_schema(conn, project):
    """The jobs.kind CHECK is the safety net for handler dispatch — verify it."""
    import sqlite3

    import pytest as pt
    with pt.raises(sqlite3.IntegrityError):
        enqueue(conn, kind="bogus_kind", project_id=project.id)


def test_handler_failure_marks_job_failed(conn, project, monkeypatch):
    """A handler that raises should mark the job failed (not crash the worker)."""
    from loci.jobs import worker as worker_mod

    def boom(_conn, _pid, _payload):
        raise RuntimeError("intentional")

    monkeypatch.setattr(worker_mod, "_handlers", lambda: {"absorb": boom})
    jid = enqueue(conn, kind="absorb", project_id=project.id)
    assert run_once(conn) is True
    job = get_job(conn, jid)
    assert job["status"] == "failed"
    assert "intentional" in (job["error"] or "")
