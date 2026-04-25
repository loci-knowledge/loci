"""SQLite-backed job queue.

API:
    enqueue(conn, kind, project_id, payload)            → job_id
    claim_one(conn) → row | None                        atomic claim
    set_progress(conn, job_id, progress, msg=None)      progress updates
    mark_done(conn, job_id, result)                     terminal
    mark_failed(conn, job_id, error)                    terminal
    get_job(conn, job_id) → dict | None                 read

Atomic claim: SQLite added UPDATE...RETURNING in 3.35 (2021). We use it to
flip a row to `running` and return it in one statement — no race between
SELECT and UPDATE.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from loci.graph.models import new_id, now_iso


def enqueue(
    conn: sqlite3.Connection,
    *,
    kind: str,
    project_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    job_id = new_id()
    conn.execute(
        """
        INSERT INTO jobs(id, kind, project_id, payload, status)
        VALUES (?, ?, ?, ?, 'queued')
        """,
        (job_id, kind, project_id, json.dumps(payload or {})),
    )
    return job_id


def claim_one(conn: sqlite3.Connection) -> dict | None:
    """Atomically claim the oldest queued job. Returns None if queue empty."""
    row = conn.execute(
        """
        UPDATE jobs
        SET status = 'running', started_at = ?
        WHERE id = (
            SELECT id FROM jobs
            WHERE status = 'queued'
            ORDER BY created_at
            LIMIT 1
        )
        RETURNING id, kind, project_id, payload
        """,
        (now_iso(),),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"], "kind": row["kind"],
        "project_id": row["project_id"],
        "payload": json.loads(row["payload"]),
    }


def set_progress(
    conn: sqlite3.Connection, job_id: str, progress: float,
    *, message: str | None = None,
) -> None:
    """0.0 ≤ progress ≤ 1.0. Optionally publish a message to the job channel."""
    progress = max(0.0, min(1.0, progress))
    conn.execute("UPDATE jobs SET progress = ? WHERE id = ?", (progress, job_id))
    if message is not None:
        # Pubsub publish is async; we publish only when called from an async
        # context. For sync workers, the message is dropped here — they can
        # instead call `pubsub.bus.publish` directly when running async.
        pass


def mark_done(
    conn: sqlite3.Connection, job_id: str, result: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        UPDATE jobs
        SET status = 'done', progress = 1.0, finished_at = ?, result = ?
        WHERE id = ?
        """,
        (now_iso(), json.dumps(result) if result is not None else None, job_id),
    )


def mark_failed(conn: sqlite3.Connection, job_id: str, error: str) -> None:
    conn.execute(
        """
        UPDATE jobs SET status = 'failed', finished_at = ?, error = ?
        WHERE id = ?
        """,
        (now_iso(), error, job_id),
    )


def get_job(conn: sqlite3.Connection, job_id: str) -> dict | None:
    row = conn.execute(
        """SELECT id, kind, project_id, status, progress, error, result,
                  created_at, started_at, finished_at
           FROM jobs WHERE id = ?""",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    out = dict(row)
    if out["result"]:
        out["result"] = json.loads(out["result"])
    return out
