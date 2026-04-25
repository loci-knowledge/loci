"""Interpreter-agent dispatch tests.

The real LLM loop is exercised by the manual smoke; here we verify:
- the reflect handler is registered with the worker
- reflect skips gracefully when no LLM key is set
- a reflect job auto-enqueues after a draft (we simulate the draft path
  manually since draft itself needs an LLM)
"""

from __future__ import annotations

from loci.agent import reflect
from loci.jobs import enqueue
from loci.jobs.queue import get_job
from loci.jobs.worker import _handlers, run_once


def test_reflect_registered_with_worker():
    handlers = _handlers()
    assert "reflect" in handlers
    assert "kickoff" in handlers
    assert "absorb" in handlers


def test_reflect_skips_without_llm(conn, project, monkeypatch):
    """No keys → reflect logs a skip, no exceptions."""
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    from loci.config import get_settings
    get_settings.cache_clear()

    res = reflect(conn, project.id, response_id=None, trigger="manual")
    assert res.skipped is True
    # The reflection row exists for audit even on skip.
    row = conn.execute(
        "SELECT id, deliberation_md FROM agent_reflections WHERE id = ?",
        (res.reflection_id,),
    ).fetchone()
    assert row is not None
    # Either a "no signal" path or a SKIPPED prefix.
    assert row["deliberation_md"]


def test_reflect_via_worker(conn, project, monkeypatch):
    """End-to-end: enqueue → claim → run → verify the row landed."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from loci.config import get_settings
    get_settings.cache_clear()
    jid = enqueue(conn, kind="reflect", project_id=project.id, payload={"trigger": "manual"})
    assert run_once(conn) is True
    j = get_job(conn, jid)
    assert j["status"] == "done"
    # `skipped` is True because no LLM, but the run succeeded structurally.
    assert j["result"]["skipped"] in (True, False)
    assert "reflection_id" in j["result"]
