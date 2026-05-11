"""sweep_interpretations job — periodic staleness enqueuer.

Finds project resources that have no interpretation yet or whose interpretation
is older than 14 days, then enqueues `infer_interpretation` jobs for them
(one per resource, with a daily-bucket fingerprint to avoid duplicates).

Payload shape:
    {
      "project_id":  "<ULID>",
      "batch_size":  20          # optional, default 20
    }

`project_id` is also available on `job["project_id"]`.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

log = logging.getLogger(__name__)

_DEFAULT_BATCH = 20


async def handle_sweep_interpretations(
    job: dict, conn: sqlite3.Connection, settings
) -> dict:
    """Find stale/missing interpretations and enqueue inference jobs.

    A resource is considered stale if:
    - It has no row in project_interpretations, OR
    - Its generated_at is older than 14 days.
    """
    payload = job.get("payload", {})
    project_id = payload.get("project_id") or job.get("project_id")

    if not project_id:
        log.warning("sweep_interpretations: job missing project_id")
        return {"skipped": "missing_project_id"}

    batch_size = int(payload.get("batch_size", _DEFAULT_BATCH))
    if batch_size < 1:
        batch_size = _DEFAULT_BATCH

    # Find resources needing (re-)interpretation.
    stale_rows = conn.execute(
        """
        SELECT pem.node_id
        FROM project_effective_members pem
        LEFT JOIN project_interpretations pi
              ON pi.project_id = pem.project_id
             AND pi.resource_id = pem.node_id
        WHERE pem.project_id = ?
          AND (
              pi.resource_id IS NULL
              OR pi.generated_at < datetime('now', '-14 days')
          )
        LIMIT ?
        """,
        (project_id, batch_size),
    ).fetchall()

    if not stale_rows:
        log.info(
            "sweep_interpretations: project=%s no stale resources found", project_id
        )
        return {"enqueued": 0}

    from loci.jobs.queue import enqueue

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    enqueued = 0

    for row in stale_rows:
        rid = row["node_id"]
        fingerprint = f"infer_interpretation:{project_id}:{rid}:{today}"
        job_id = enqueue(
            conn,
            kind="infer_interpretation",
            project_id=project_id,
            payload={
                "resource_id": rid,
                "project_id": project_id,
                "trigger": "sweep",
            },
            fingerprint=fingerprint,
        )
        if job_id is not None:
            enqueued += 1

    log.info(
        "sweep_interpretations: project=%s enqueued=%d (batch_size=%d)",
        project_id,
        enqueued,
        batch_size,
    )
    return {"enqueued": enqueued}
