"""Proposal queue endpoint.

PLAN.md §API §Graph manipulation:

    GET  /projects/:id/proposals     the proposal queue
"""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, Query

from loci.api.dependencies import db, project_by_id
from loci.graph.models import Project

router = APIRouter(prefix="/projects", tags=["proposals"])


@router.get("/{project_id}/proposals")
def list_proposals(
    project: Project = Depends(project_by_id),
    status_filter: str = Query("pending", alias="status"),
    limit: int = Query(50, le=500),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    rows = conn.execute(
        """
        SELECT id, kind, payload, status, fingerprint, created_at, resolved_at
        FROM proposals
        WHERE project_id = ? AND status = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (project.id, status_filter, limit),
    ).fetchall()
    return {
        "proposals": [
            {
                "id": r["id"], "kind": r["kind"],
                "payload": json.loads(r["payload"]),
                "status": r["status"], "fingerprint": r["fingerprint"],
                "created_at": r["created_at"], "resolved_at": r["resolved_at"],
            }
            for r in rows
        ],
    }
