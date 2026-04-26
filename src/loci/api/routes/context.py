"""Project context endpoints — awareness surface for connected clients."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query

from loci.api.dependencies import db, project_by_id
from loci.graph.models import Project
from loci.graph.workspaces import WorkspaceRepository

router = APIRouter(prefix="/projects", tags=["context"])


@router.get("/{project_id}/context")
def get_project_context(
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    """Return project info, linked workspaces, stats, and recent activity."""
    ws_repo = WorkspaceRepository(conn)
    links = ws_repo.linked_workspaces(project.id)

    workspaces = []
    for ws, link in links:
        if link.role == "excluded":
            continue
        raw_count = conn.execute(
            "SELECT COUNT(*) FROM nodes n JOIN workspace_membership wm ON wm.node_id = n.id "
            "WHERE wm.workspace_id = ? AND n.kind = 'raw'",
            (ws.id,),
        ).fetchone()[0]
        workspaces.append({
            "id": ws.id, "slug": ws.slug, "name": ws.name,
            "kind": ws.kind, "role": link.role, "weight": link.weight,
            "raw_count": raw_count,
            "description_md": ws.description_md,
            "last_scanned_at": ws.last_scanned_at,
        })

    stats_row = conn.execute(
        """
        SELECT
            SUM(CASE n.kind WHEN 'raw' THEN 1 ELSE 0 END) AS raw_nodes,
            SUM(CASE n.kind WHEN 'interpretation' THEN 1 ELSE 0 END) AS interpretation_nodes,
            SUM(CASE n.status WHEN 'live' THEN 1 ELSE 0 END) AS live_nodes
        FROM nodes n
        JOIN project_effective_members pm ON pm.node_id = n.id
        WHERE pm.project_id = ?
        """,
        (project.id,),
    ).fetchone()

    edge_count = conn.execute(
        """
        SELECT COUNT(*) FROM edges
        WHERE src IN (SELECT node_id FROM project_effective_members WHERE project_id = ?)
        """,
        (project.id,),
    ).fetchone()[0]

    recent = conn.execute(
        """
        SELECT n.id AS node_id, n.title, n.kind, n.subkind,
               n.last_accessed_at, n.access_count, n.confidence
        FROM nodes n
        JOIN project_effective_members pm ON pm.node_id = n.id
        WHERE pm.project_id = ?
          AND n.last_accessed_at IS NOT NULL
        ORDER BY n.last_accessed_at DESC
        LIMIT 10
        """,
        (project.id,),
    ).fetchall()

    return {
        "project": {
            "id": project.id, "slug": project.slug, "name": project.name,
            "profile_md": project.profile_md, "last_active_at": project.last_active_at,
        },
        "workspaces": workspaces,
        "stats": {
            "raw_nodes": stats_row["raw_nodes"] or 0,
            "interpretation_nodes": stats_row["interpretation_nodes"] or 0,
            "live_nodes": stats_row["live_nodes"] or 0,
            "edges": edge_count,
        },
        "recent_activity": [
            {
                "node_id": r["node_id"], "title": r["title"],
                "kind": r["kind"], "subkind": r["subkind"],
                "last_accessed_at": r["last_accessed_at"],
                "access_count": r["access_count"],
                "confidence": r["confidence"],
            }
            for r in recent
        ],
    }


@router.get("/{project_id}/recent-nodes")
def get_recent_nodes(
    project: Project = Depends(project_by_id),
    hours: int = Query(24, ge=1, le=168),
    kind: str | None = Query(None),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    """Return nodes created or updated within the last N hours (default 24)."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    kind_clause = "AND n.kind = ?" if kind else ""
    params: tuple = (project.id, since_iso, since_iso, kind) if kind else (project.id, since_iso, since_iso)

    rows = conn.execute(
        f"""
        SELECT n.id, n.kind, n.subkind, n.title, n.body,
               n.confidence, n.status, n.created_at, n.updated_at
        FROM nodes n
        JOIN project_effective_members pm ON pm.node_id = n.id
        WHERE pm.project_id = ?
          AND (n.created_at >= ? OR n.updated_at >= ?)
          {kind_clause}
        ORDER BY n.updated_at DESC
        LIMIT 50
        """,
        params,
    ).fetchall()

    return {
        "nodes": [
            {
                "id": r["id"], "kind": r["kind"], "subkind": r["subkind"],
                "title": r["title"], "body": r["body"] or "",
                "confidence": r["confidence"], "status": r["status"],
                "created_at": r["created_at"], "updated_at": r["updated_at"],
            }
            for r in rows
        ],
        "since": since_iso,
        "hours": hours,
    }
