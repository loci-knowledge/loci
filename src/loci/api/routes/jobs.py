"""Job endpoints.

Routes:
    POST /projects/:id/absorb        enqueue absorb checkpoint
    POST /projects/:id/reflect       enqueue reflect (optional ?absorb=true)
    POST /projects/:id/autoresearch  enqueue autoresearch job
    GET  /jobs/:id                   status

The actual queue + worker live in `loci/jobs/`. This route handles HTTP submit + poll.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

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


@router.post("/projects/{project_id}/reflect", status_code=202)
def enqueue_reflect(
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
    absorb: bool = Query(False, description="Run absorb checkpoint before reflect"),
) -> dict:
    from loci.jobs.queue import enqueue
    if absorb:
        enqueue(conn, kind="absorb", project_id=project.id, payload={})
    job_id = enqueue(conn, kind="reflect", project_id=project.id, payload={})
    return {"job_id": job_id, "status": "queued"}


class AutoresearchRequest(BaseModel):
    query: str
    workspace_id: str
    hf_owner: str | None = None
    hardware: str | None = None
    sandbox: bool = False
    max_iterations: int = 30


@router.post("/projects/{project_id}/autoresearch", status_code=202)
def enqueue_autoresearch(
    body: AutoresearchRequest,
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    from loci.jobs.queue import enqueue
    payload: dict[str, Any] = {
        "query": body.query,
        "workspace_id": body.workspace_id,
        "hf_owner": body.hf_owner,
        "hardware": body.hardware,
        "sandbox": body.sandbox,
        "max_iterations": body.max_iterations,
    }
    job_id = enqueue(conn, kind="autoresearch", project_id=project.id, payload=payload)
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
