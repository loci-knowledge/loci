"""Revision history and revert endpoints for interpretation nodes.

    GET  /nodes/:id/revisions              list revision history
    POST /nodes/:id/revisions/:rev/revert  revert to a prior revision
"""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from loci.api.dependencies import db
from loci.api.publishers import publish_node_upsert
from loci.graph import NodeRepository

router = APIRouter(tags=["revisions"])


@router.get("/nodes/{node_id}/revisions")
def list_revisions(
    node_id: str,
    limit: int = 50,
    conn: sqlite3.Connection = Depends(db),
) -> list[dict]:
    """Return up to `limit` revisions for `node_id`, most-recent first."""
    try:
        rows = conn.execute(
            """
            SELECT id, node_id, ts, actor, source_tool, op, reason,
                   prior_values, new_values, parent_revision_id
            FROM node_revisions
            WHERE node_id = ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (node_id, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        # Table doesn't exist on older DBs — return empty gracefully.
        return []
    return [dict(row) for row in rows]


@router.post("/nodes/{node_id}/revisions/{revision_id}/revert")
def revert_node(
    node_id: str,
    revision_id: str,
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    """Revert a node's locus slots to the state captured in `revision_id`."""
    try:
        row = conn.execute(
            "SELECT * FROM node_revisions WHERE id = ? AND node_id = ?",
            (revision_id, node_id),
        ).fetchone()
    except sqlite3.OperationalError:
        raise HTTPException(status_code=404, detail="revision not found") from None

    if row is None:
        raise HTTPException(status_code=404, detail="revision not found")

    try:
        prior_values: dict = json.loads(row["prior_values"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="revision prior_values is not valid JSON") from None

    # Extract only the locus fields that update_locus accepts.
    locus_fields = ("relation_md", "overlap_md", "source_anchor_md", "angle")
    kwargs = {k: prior_values[k] for k in locus_fields if k in prior_values and prior_values[k] is not None}

    nodes_repo = NodeRepository(conn)
    n = nodes_repo.get(node_id)
    if n is None:
        raise HTTPException(status_code=404, detail="node not found")

    nodes_repo.update_locus(
        node_id,
        **kwargs,
        actor="user",
        source_tool="api.revert",
        reason=f"revert to revision {revision_id}",
    )

    # Publish the updated node to all subscribed project channels.
    updated = nodes_repo.get(node_id)
    if updated is not None:
        publish_node_upsert(conn, updated)

    # Fetch the new revision id that update_locus just inserted.
    try:
        new_rev_row = conn.execute(
            """
            SELECT id FROM node_revisions
            WHERE node_id = ?
            ORDER BY ts DESC
            LIMIT 1
            """,
            (node_id,),
        ).fetchone()
        new_revision_id = new_rev_row["id"] if new_rev_row else None
    except sqlite3.OperationalError:
        new_revision_id = None

    return {"ok": True, "revision_id": new_revision_id}
