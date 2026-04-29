"""Job endpoints (v2).

Routes:
    POST /projects/:id/classify-aspects   enqueue aspect classification for a resource
    POST /projects/:id/parse-links        enqueue link parsing for a resource
    GET  /jobs/:id                        status
"""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from loci.api.dependencies import db, project_by_id
from loci.graph.models import Project

router = APIRouter(tags=["jobs"])


class ClassifyAspectsRequest(BaseModel):
    resource_id: str


@router.post("/projects/{project_id}/classify-aspects", status_code=202)
def enqueue_classify_aspects(
    body: ClassifyAspectsRequest,
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    from loci.jobs.queue import enqueue
    job_id = enqueue(conn, kind="classify_aspects", project_id=project.id,
                     payload={"resource_id": body.resource_id, "project_id": project.id})
    return {"job_id": job_id, "status": "queued"}


class ParseLinksRequest(BaseModel):
    resource_id: str


@router.post("/projects/{project_id}/parse-links", status_code=202)
def enqueue_parse_links(
    body: ParseLinksRequest,
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    from loci.jobs.queue import enqueue
    job_id = enqueue(conn, kind="parse_links", project_id=project.id,
                     payload={"resource_id": body.resource_id, "project_id": project.id})
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
