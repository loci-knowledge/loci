"""Side-effect surfacing for retrieve / draft.

Both endpoints can enqueue a `reflect` job after they run — the interpreter
agent replays the response and may mutate the graph (propose loci, adjust
confidences, strengthen citations). Without exposing this, the user sees
their query come back as data while the graph silently changes underneath.

This module owns:
  * `maybe_enqueue_retrieve_reflect` — cooldown-gated enqueue for retrieve
  * `pending_effects_from_reflect`   — pack a reflect job_id into the
                                       `pending_effects` response field
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from loci.jobs import enqueue

# 5-minute cooldown stops retrieve from spawning a reflect on every call.
# Draft ALWAYS enqueues a reflect (drafts are higher-signal) so this only
# governs the retrieve path.
_REFLECT_COOLDOWN_SECONDS = 300


def maybe_enqueue_retrieve_reflect(
    conn: sqlite3.Connection, project_id: str, response_id: str,
) -> str | None:
    """Enqueue a lightweight reflect after retrieve, with cooldown.

    Returns the job_id when a reflect was enqueued, None when the cooldown
    suppressed it. Callers wrap the result in `pending_effects` so the user
    can see when retrieval triggered a graph mutation.
    """
    last = conn.execute(
        "SELECT MAX(ts) FROM agent_reflections WHERE project_id = ?",
        (project_id,),
    ).fetchone()[0]
    if last:
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            elapsed = (datetime.now(UTC) - last_dt).total_seconds()
            if elapsed < _REFLECT_COOLDOWN_SECONDS:
                return None
        except Exception:  # noqa: BLE001
            pass
    return enqueue(
        conn, kind="reflect", project_id=project_id,
        payload={"response_id": response_id, "trigger": "retrieve", "lightweight": True},
    )


def pending_effects_from_reflect(
    reflect_job_id: str | None, *, trigger: str,
) -> list[dict[str, Any]]:
    """Pack a reflect job_id into the `pending_effects` response field.

    Empty list when no reflect was enqueued (e.g. cooldown suppressed it).
    Shape kept open: future graph-mutating side-effects can extend this list.
    """
    if not reflect_job_id:
        return []
    return [{
        "kind": "reflect_job",
        "job_id": reflect_job_id,
        "trigger": trigger,
        "purpose": (
            "the interpreter agent will replay this response and may propose "
            "new loci, strengthen citations, or adjust confidences based on "
            "which loci routed which raws"
        ),
    }]
