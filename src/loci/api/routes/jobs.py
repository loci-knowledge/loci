"""Job endpoints.

PLAN.md §API §Background jobs:

    POST /projects/:id/absorb        enqueue a checkpoint
    GET  /jobs/:id                   status

The actual queue + worker live in `loci/jobs/` (Phase 9). This route only
deals with HTTP-level submit + poll.
"""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from loci.api.dependencies import db, project_by_id
from loci.graph.models import Project

router = APIRouter(tags=["jobs"])


@router.post("/projects/{project_id}/absorb", status_code=202)
def enqueue_absorb(
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    from loci.jobs.queue import enqueue
    job_id = enqueue(conn, kind="absorb", project_id=project.id, payload={})
    return {"job_id": job_id, "status": "queued"}


@router.post("/projects/{project_id}/kickoff", status_code=202)
def enqueue_kickoff(
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
    n: int = 8,
) -> dict:
    """Enqueue the kickoff job — generate question proposals from the project
    profile + a sample of registered raws."""
    from loci.jobs.queue import enqueue
    job_id = enqueue(conn, kind="kickoff", project_id=project.id, payload={"n": n})
    return {"job_id": job_id, "status": "queued"}


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str, conn: sqlite3.Connection = Depends(db),
) -> dict:
    row = conn.execute(
        """SELECT id, kind, project_id, status, progress, error, result,
                  created_at, started_at, finished_at
           FROM jobs WHERE id = ?""",
        (job_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, detail="job not found")
    return {
        "id": row["id"], "kind": row["kind"], "project_id": row["project_id"],
        "status": row["status"], "progress": row["progress"],
        "error": row["error"],
        "result": json.loads(row["result"]) if row["result"] else None,
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }
