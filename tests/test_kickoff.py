"""Kickoff job tests.

Without an LLM key configured, the kickoff job runs but produces no
proposals (it surfaces a `skipped: true` reason). We verify that path
and the dispatch wiring; the real LLM path is exercised in manual smoke
tests with a key set.
"""

from __future__ import annotations

from loci.graph import ProjectRepository
from loci.ingest import scan_path
from loci.jobs import enqueue
from loci.jobs.queue import get_job
from loci.jobs.worker import run_once


def test_kickoff_skips_without_llm(conn, fake_embedder, project, workspace, corpus_dir, monkeypatch):
    """Without an LLM the kickoff handler returns skipped, no failure."""
    import loci.jobs.kickoff as kickoff_mod
    from loci.llm import LLMNotConfiguredError
    monkeypatch.setattr(kickoff_mod, "build_agent", lambda *a, **kw: (_ for _ in ()).throw(LLMNotConfiguredError("no key")))

    # Need a profile and some raws so the handler doesn't no-op for empty input.
    ProjectRepository(conn).update_profile(project.id, "Survey of attention variants.")
    scan_path(conn, workspace.id, corpus_dir, embedder=fake_embedder)

    jid = enqueue(conn, kind="kickoff", project_id=project.id, payload={"n": 5})
    assert run_once(conn) is True
    job = get_job(conn, jid)
    assert job["status"] == "done"
    assert job["result"]["skipped"] is True
    assert job["result"]["proposals"] == 0


def test_kickoff_no_input_returns_skip(conn, project, monkeypatch):
    """Empty profile + no raws → skip (no LLM call attempted)."""
    import loci.jobs.kickoff as kickoff_mod
    from loci.llm import LLMNotConfiguredError
    monkeypatch.setattr(kickoff_mod, "build_agent", lambda *a, **kw: (_ for _ in ()).throw(LLMNotConfiguredError("no key")))
    jid = enqueue(conn, kind="kickoff", project_id=project.id, payload={})
    run_once(conn)
    job = get_job(conn, jid)
    assert job["status"] == "done"
    assert job["result"]["skipped"] is True


def test_kickoff_handler_dispatch(conn, project, monkeypatch):
    """Verify the worker dispatch table includes 'kickoff'."""
    from loci.jobs.worker import _handlers
    handlers = _handlers()
    assert "kickoff" in handlers
    assert "absorb" in handlers
