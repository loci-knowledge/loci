"""Revision log for interpretation nodes.

Every edit or delete appends a row to `node_revisions`. The materialised slot
values live in `interpretation_nodes` as before; this module records *what
changed* and the prior state, enabling history review, undo, and personalization
training data collection.

Usage — call from inside NodeRepository._txn() *after* capturing the prior
snapshot but *before* (or after, within the same transaction) running the UPDATE:

    snapshot = capture_locus_snapshot(self.conn, node_id)
    # ... run UPDATE ...
    RevisionLogger(self.conn).log_update_locus(
        node_id, prior=snapshot, new_values={...}, actor="user", source_tool="mcp.loci_edit_locus"
    )
"""

from __future__ import annotations

import json
import sqlite3

import ulid

from loci.graph.models import now_iso


def capture_locus_snapshot(conn: sqlite3.Connection, node_id: str) -> dict:
    """Capture a full slot snapshot of an interpretation node (or empty dict for raw nodes)."""
    row = conn.execute(
        """
        SELECT n.title, n.body,
               i.relation_md, i.overlap_md, i.source_anchor_md, i.angle, i.rationale_md,
               i.origin
        FROM nodes n
        LEFT JOIN interpretation_nodes i ON i.node_id = n.id
        WHERE n.id = ?
        """,
        (node_id,),
    ).fetchone()
    if row is None:
        return {}
    return {
        "title": row["title"],
        "body": row["body"],
        "relation_md": row["relation_md"],
        "overlap_md": row["overlap_md"],
        "source_anchor_md": row["source_anchor_md"],
        "angle": row["angle"],
        "rationale_md": row["rationale_md"],
        "origin": row["origin"],
    }


def capture_edge_snapshot(conn: sqlite3.Connection, node_id: str) -> list[dict]:
    """Capture incident edges for a node — used in hard_delete tombstone."""
    rows = conn.execute(
        """
        SELECT id, src, dst, type, weight, created_by, rationale, angle
        FROM edges WHERE src = ? OR dst = ?
        """,
        (node_id, node_id),
    ).fetchall()
    return [dict(r) for r in rows]


class RevisionLogger:
    """Appends rows to `node_revisions`. Constructed with an open connection.

    All methods are sync and must be called inside the caller's transaction so
    that the revision row is rolled back if the surrounding UPDATE fails.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def _insert(
        self,
        node_id: str,
        op: str,
        prior: dict,
        new: dict,
        *,
        actor: str = "system",
        source_tool: str | None = None,
        reason: str | None = None,
        parent_revision_id: str | None = None,
    ) -> str:
        rid = str(ulid.new())
        self.conn.execute(
            """
            INSERT INTO node_revisions
                (id, node_id, ts, actor, source_tool, op, reason,
                 prior_values, new_values, parent_revision_id)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                rid, node_id, now_iso(), actor, source_tool, op, reason,
                json.dumps(prior, ensure_ascii=False),
                json.dumps(new, ensure_ascii=False),
                parent_revision_id,
            ),
        )
        return rid

    def log_create(
        self,
        node_id: str,
        new_values: dict,
        *,
        actor: str = "system",
        source_tool: str | None = None,
        reason: str | None = None,
    ) -> str:
        return self._insert(node_id, "create", {}, new_values,
                             actor=actor, source_tool=source_tool, reason=reason)

    def log_update_locus(
        self,
        node_id: str,
        prior: dict,
        new_values: dict,
        *,
        actor: str = "system",
        source_tool: str | None = None,
        reason: str | None = None,
    ) -> str:
        return self._insert(node_id, "update_locus", prior, new_values,
                             actor=actor, source_tool=source_tool, reason=reason)

    def log_update_body(
        self,
        node_id: str,
        prior: dict,
        new_values: dict,
        *,
        actor: str = "system",
        source_tool: str | None = None,
        reason: str | None = None,
    ) -> str:
        return self._insert(node_id, "update_body", prior, new_values,
                             actor=actor, source_tool=source_tool, reason=reason)

    def log_set_angle(
        self,
        node_id: str,
        prior: dict,
        new_values: dict,
        *,
        actor: str = "system",
        source_tool: str | None = None,
        reason: str | None = None,
    ) -> str:
        return self._insert(node_id, "set_angle", prior, new_values,
                             actor=actor, source_tool=source_tool, reason=reason)

    def log_hard_delete(
        self,
        node_id: str,
        prior: dict,
        *,
        actor: str = "system",
        source_tool: str | None = None,
        reason: str | None = None,
    ) -> str:
        return self._insert(node_id, "hard_delete", prior, {"deleted": True},
                             actor=actor, source_tool=source_tool, reason=reason)

    def log_revert(
        self,
        node_id: str,
        prior: dict,
        new_values: dict,
        *,
        actor: str = "user",
        source_tool: str | None = None,
        reason: str | None = None,
        parent_revision_id: str | None = None,
    ) -> str:
        return self._insert(node_id, "revert", prior, new_values,
                             actor=actor, source_tool=source_tool, reason=reason,
                             parent_revision_id=parent_revision_id)
