"""Job queue tests (v2 job kinds)."""

from __future__ import annotations

from loci.jobs import enqueue
from loci.jobs.queue import claim_one, get_job
from loci.jobs.worker import run_once


def test_enqueue_and_claim(conn, project):
    jid = enqueue(conn, kind="log_usage", project_id=project.id, payload={"resource_id": "x"})
    job = claim_one(conn)
    assert job["id"] == jid
    assert job["kind"] == "log_usage"
    assert job["payload"] == {"resource_id": "x"}
    # Second claim returns None (queue empty)
    assert claim_one(conn) is None


def test_unknown_kind_handled_by_worker(conn, project):
    """jobs.kind has no SQL CHECK (removed by _m001); worker marks unknown kinds failed."""
    # Since v2.1, kind validation is Python-level in the worker dispatch —
    # the DB accepts any kind, the worker marks it failed when no handler found.
    jid = enqueue(conn, kind="bogus_kind", project_id=project.id)
    assert jid is not None  # SQL level accepts it
    # Worker claims and fails it (no handler registered)
    assert run_once(conn) is True
    job = get_job(conn, jid)
    assert job["status"] == "failed"


def test_handler_failure_marks_job_failed(conn, project, monkeypatch):
    """A handler that raises should mark the job failed (not crash the worker)."""
    from loci.jobs import worker as worker_mod

    async def boom(job, conn, settings):
        raise RuntimeError("intentional")

    monkeypatch.setattr(worker_mod, "_handlers", lambda: {"log_usage": boom})
    jid = enqueue(conn, kind="log_usage", project_id=project.id, payload={"resource_id": "x"})
    assert run_once(conn) is True
    job = get_job(conn, jid)
    assert job["status"] == "failed"
    assert "intentional" in (job["error"] or "")
