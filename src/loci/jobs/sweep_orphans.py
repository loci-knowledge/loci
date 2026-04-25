"""Sweep-orphans job — mark interpretation nodes dirty when their evidence
raws leave a project's effective membership.

Triggered when a workspace is unlinked from a project. Walks all live
interpretation nodes in the project that cite raws no longer reachable via
`project_effective_members`, flips their status to `dirty`, and files a
`forget` proposal for each.

No data is deleted. The user reviews proposals and accepts/dismisses via the
existing `loci_accept_proposal` MCP tool path.

Payload shape:
    {
      "workspace_id": "<ULID>"   # the workspace that was just unlinked
    }
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3

from loci.graph.models import new_id, now_iso

log = logging.getLogger(__name__)


def _fingerprint(project_id: str, node_id: str) -> str:
    canonical = json.dumps({"kind": "node", "about_node_id": node_id,
                            "project_id": project_id, "action": "dismiss",
                            "trigger": "sweep_orphans"}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def run(conn: sqlite3.Connection, project_id: str | None, payload: dict) -> dict:
    """Sweep-orphans handler. Signature matches worker dispatch convention."""
    if project_id is None:
        raise ValueError("sweep_orphans requires a project_id")

    workspace_id = payload.get("workspace_id")
    if workspace_id is None:
        raise ValueError("sweep_orphans requires workspace_id in payload")

    # Find live interpretation nodes in this project whose only raw citations
    # (via `cites` edges) are no longer in project_effective_members.
    # A node is "orphaned" if ALL its cited raws left the project.
    orphaned = conn.execute(
        """
        SELECT DISTINCT n.id, n.title
        FROM nodes n
        JOIN project_effective_members pm ON pm.node_id = n.id
        WHERE pm.project_id = ?
          AND n.kind = 'interpretation'
          AND n.status = 'live'
          -- Has at least one cites edge
          AND EXISTS (
              SELECT 1 FROM edges e
              WHERE e.src = n.id AND e.type = 'cites'
          )
          -- But NONE of its cites targets are still in effective members
          AND NOT EXISTS (
              SELECT 1 FROM edges e
              JOIN project_effective_members pm2 ON pm2.node_id = e.dst
              WHERE e.src = n.id
                AND e.type = 'cites'
                AND pm2.project_id = ?
          )
        """,
        (project_id, project_id),
    ).fetchall()

    if not orphaned:
        return {"swept": 0, "proposals_filed": 0}

    now = now_iso()
    proposals_filed = 0
    for r in orphaned:
        node_id = r["id"]
        title = r["title"] or "(untitled)"
        try:
            conn.execute(
                "UPDATE nodes SET status = 'dirty', updated_at = ? WHERE id = ?",
                (now, node_id),
            )
            fp = _fingerprint(project_id, node_id)
            proposal_payload = {
                "about_node_id": node_id,
                "action": "dismiss",
                "title": title,
                "reason": "Evidence raws left project after workspace unlink.",
            }
            try:
                conn.execute(
                    """
                    INSERT INTO proposals(id, project_id, kind, payload, status, fingerprint)
                    VALUES (?, ?, 'node', ?, 'pending', ?)
                    """,
                    (new_id(), project_id, json.dumps(proposal_payload), fp),
                )
                proposals_filed += 1
            except Exception:  # noqa: BLE001 — IntegrityError = already proposed
                pass
        except Exception as exc:  # noqa: BLE001
            log.warning("sweep_orphans: failed for node %s: %s", node_id, exc)

    return {"swept": len(orphaned), "proposals_filed": proposals_filed}
