"""Reflect-job handler — wraps the interpreter agent in a job.

Auto-enqueued by `loci.draft.draft()` after every draft response. Also
fires after a citation-feedback submission, and can be invoked manually via
`POST /projects/:id/reflect`.

Payload shape:
    {
      "response_id": "<ULID>",      # optional
      "trigger":     "draft" | "feedback" | "manual" | "kickoff"
    }
"""

from __future__ import annotations

import logging
import sqlite3

from loci.agent import reflect

log = logging.getLogger(__name__)


def run(conn: sqlite3.Connection, project_id: str | None, payload: dict) -> dict:
    if project_id is None:
        raise ValueError("reflect requires a project_id")
    response_id = payload.get("response_id")
    trigger = payload.get("trigger") or "manual"
    if trigger not in {"draft", "feedback", "manual", "kickoff"}:
        trigger = "manual"
    res = reflect(conn, project_id, response_id=response_id, trigger=trigger)
    return {
        "reflection_id": res.reflection_id,
        "actions_taken": res.actions_taken,
        "actions_dropped": res.actions_dropped,
        "skipped": res.skipped,
        "skip_reason": res.skip_reason,
    }
