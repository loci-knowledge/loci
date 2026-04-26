"""Graph view endpoint for the planned VSCode extension.

PLAN.md §API §Graph manipulation:

    GET  /projects/:id/graph         nodes + edges, with layout hints

We don't compute layout server-side — clients use a force-directed layout
(d3-force or cytoscape) anyway. We do return a compact shape: just the fields
the visualizer needs, not the full body text.

The response also carries `community_version` (epoch-seconds of the latest
community snapshot for this project) so the frontend can detect when it
needs to re-district. Each node carries `community_id` denormalised from
the latest community snapshot — saves the frontend a follow-up join.
"""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, Query

from loci.api.dependencies import db, project_by_id
from loci.api.routes.projects import _latest_communities, _snapshot_at_to_version
from loci.graph.models import Project

router = APIRouter(prefix="/projects", tags=["graph"])


@router.get("/{project_id}/graph")
def get_graph(
    project: Project = Depends(project_by_id),
    include_raw: bool = Query(True),
    statuses: list[str] = Query(["live", "dirty"]),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    placeholders = ",".join("?" * len(statuses))
    kind_clause = "" if include_raw else "AND n.kind = 'interpretation'"
    # A node may appear under multiple sources ('workspace' + 'pinned') when it
    # is both in a linked workspace and explicitly pinned. Aggregate to the
    # highest-priority source: pinned > override > workspace.
    # Pull the locus slots (relation/overlap/anchor + angle) alongside the
    # base shape so the visualizer can render them on click without a
    # follow-up call.
    rows = conn.execute(
        f"""
        SELECT n.id, n.kind, n.subkind, n.title, n.body, n.confidence, n.status,
               n.access_count, n.last_accessed_at,
               i.relation_md, i.overlap_md, i.source_anchor_md, i.angle,
               CASE
                 WHEN SUM(CASE pm.source WHEN 'pinned'   THEN 1 ELSE 0 END) > 0 THEN 'pinned'
                 WHEN SUM(CASE pm.source WHEN 'override' THEN 1 ELSE 0 END) > 0 THEN 'override'
                 ELSE 'workspace'
               END AS role
        FROM nodes n
        JOIN project_effective_members pm ON pm.node_id = n.id
        LEFT JOIN interpretation_nodes i ON i.node_id = n.id
        WHERE pm.project_id = ?
          AND n.status IN ({placeholders})
          {kind_clause}
        GROUP BY n.id
        """,
        (project.id, *statuses),
    ).fetchall()
    node_ids = {r["id"] for r in rows}

    # Compute the latest community snapshot once and fold each node's
    # membership in. Older snapshots are ignored for the live graph view.
    community_rows, snapshot_at = _latest_communities(conn, project.id)
    node_to_community: dict[str, str] = {}
    for c in community_rows:
        try:
            for member_id in json.loads(c["member_node_ids"]):
                node_to_community[member_id] = c["id"]
        except (TypeError, ValueError):
            continue
    community_version = _snapshot_at_to_version(snapshot_at)

    if not node_ids:
        return {"nodes": [], "edges": [], "community_version": community_version}

    placeholders_n = ",".join("?" * len(node_ids))
    edge_rows = conn.execute(
        f"""
        SELECT id, src, dst, type, weight
        FROM edges
        WHERE src IN ({placeholders_n}) AND dst IN ({placeholders_n})
        """,
        (*node_ids, *node_ids),
    ).fetchall()

    return {
        "nodes": [
            {
                "id": r["id"], "kind": r["kind"], "subkind": r["subkind"],
                "title": r["title"], "body": r["body"] or "",
                "relation_md": r["relation_md"] or "",
                "overlap_md": r["overlap_md"] or "",
                "source_anchor_md": r["source_anchor_md"] or "",
                "angle": r["angle"],
                "confidence": r["confidence"],
                "status": r["status"], "access_count": r["access_count"],
                "last_accessed_at": r["last_accessed_at"],
                "role": r["role"],
                "community_id": node_to_community.get(r["id"]),
            }
            for r in rows
        ],
        "edges": [
            {
                "id": r["id"], "src": r["src"], "dst": r["dst"],
                "type": r["type"], "weight": r["weight"],
            }
            for r in edge_rows
        ],
        "community_version": community_version,
    }
