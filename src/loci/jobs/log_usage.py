"""log_usage job — flush a usage event to resource_usage_log.

Very lightweight. If the resource now has 5+ usage events, also queues a
classify_aspects job to refine its aspects based on accumulated usage context.

Payload shape:
    {
      "resource_id":    "<ULID>",    # required
      "session_hash":   "...",       # optional
      "tool_call_type": "...",       # optional — e.g. "retrieve", "draft"
      "context_note":   "...",       # optional — free-form annotation
    }

`project_id` is taken from job["project_id"] (set at enqueue time by the caller).
"""

from __future__ import annotations

import logging
import sqlite3

from loci.graph.models import new_id, now_iso

log = logging.getLogger(__name__)

# Threshold at which we queue a classify_aspects refinement pass.
_CLASSIFY_THRESHOLD = 5


async def handle_log_usage(job: dict, conn: sqlite3.Connection, settings) -> dict:
    """Insert a resource_usage_log row and optionally trigger aspect refinement.

    Steps:
    1. Insert a row into resource_usage_log.
    2. Count total usage events for this resource.
    3. If count reaches the threshold (5), enqueue a classify_aspects job
       so the aspect labels can be refined with usage context.
    """
    payload = job.get("payload", {})
    resource_id = payload.get("resource_id")

    if not resource_id:
        raise ValueError("log_usage: payload missing resource_id")

    project_id = job.get("project_id")  # may be None
    session_hash = payload.get("session_hash")
    tool_call_type = payload.get("tool_call_type")
    context_note = payload.get("context_note")

    # 1. Insert the usage event.
    usage_id = new_id()
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO resource_usage_log(id, resource_id, session_hash,
                                       tool_call_type, context_note, used_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (usage_id, resource_id, session_hash, tool_call_type, context_note, ts),
    )

    # 2. Count total usage events for this resource.
    count_row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM resource_usage_log WHERE resource_id = ?",
        (resource_id,),
    ).fetchone()
    usage_count = count_row["cnt"] if count_row else 0

    # 3. Queue a classify_aspects refinement if we hit the threshold.
    classify_queued = False
    if usage_count >= _CLASSIFY_THRESHOLD and project_id is not None:
        # Use a fingerprint to avoid stacking up multiple classify jobs for the
        # same resource when usage bursts past the threshold repeatedly.
        import hashlib
        fingerprint = hashlib.sha256(
            f"classify_aspects:{resource_id}:{usage_count // _CLASSIFY_THRESHOLD}".encode()
        ).hexdigest()[:32]

        from loci.jobs.queue import enqueue
        enqueue(
            conn,
            kind="classify_aspects",
            project_id=project_id,
            payload={"resource_id": resource_id, "project_id": project_id},
            fingerprint=fingerprint,
        )
        classify_queued = True
        log.info(
            "log_usage: usage_count=%d for resource=%s; queued classify_aspects",
            usage_count, resource_id,
        )

    return {
        "usage_id": usage_id,
        "resource_id": resource_id,
        "usage_count": usage_count,
        "classify_aspects_queued": classify_queued,
    }
