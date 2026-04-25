"""Information Workspace repositories.

An InformationWorkspace is a named, typed bag of source roots (a peer of
Project). Projects and Workspaces are linked M:N via project_workspaces.
A workspace's raw nodes flow into every linked project's effective membership
via the project_effective_members view.

WorkspaceRepository owns:
  - Workspace CRUD
  - workspace_sources management (root paths)
  - workspace_membership writes (populated by the ingest pipeline)
  - project ↔ workspace link/unlink

A companion method ProjectRepository.effective_members() reads the derived
project_effective_members view to return the full node set for a project.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from loci.graph.models import (
    ProjectWorkspace,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceSource,
    new_id,
    now_iso,
)


class WorkspaceRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -----------------------------------------------------------------------
    # Workspace CRUD
    # -----------------------------------------------------------------------

    def create(self, workspace: Workspace) -> Workspace:
        self.conn.execute(
            """
            INSERT INTO information_workspaces(id, slug, name, description_md, kind,
                                               created_at, last_active_at,
                                               last_scanned_at, config)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace.id, workspace.slug, workspace.name, workspace.description_md,
                workspace.kind, workspace.created_at, workspace.last_active_at,
                workspace.last_scanned_at, json.dumps(workspace.config),
            ),
        )
        return workspace

    def get(self, workspace_id: str) -> Workspace | None:
        row = self.conn.execute(
            "SELECT * FROM information_workspaces WHERE id = ?", (workspace_id,)
        ).fetchone()
        return self._row_to_workspace(row) if row else None

    def get_by_slug(self, slug: str) -> Workspace | None:
        row = self.conn.execute(
            "SELECT * FROM information_workspaces WHERE slug = ?", (slug,)
        ).fetchone()
        return self._row_to_workspace(row) if row else None

    def list(self) -> list[Workspace]:
        rows = self.conn.execute(
            "SELECT * FROM information_workspaces ORDER BY last_active_at DESC"
        ).fetchall()
        return [self._row_to_workspace(r) for r in rows]

    def touch(self, workspace_id: str) -> None:
        self.conn.execute(
            "UPDATE information_workspaces SET last_active_at = ? WHERE id = ?",
            (now_iso(), workspace_id),
        )

    def mark_scanned(self, workspace_id: str) -> None:
        self.conn.execute(
            "UPDATE information_workspaces SET last_scanned_at = ? WHERE id = ?",
            (now_iso(), workspace_id),
        )

    # -----------------------------------------------------------------------
    # Source roots
    # -----------------------------------------------------------------------

    def add_source(
        self, workspace_id: str, root: Path, *, label: str | None = None,
    ) -> WorkspaceSource:
        """Register a root path for the workspace. Idempotent on (workspace_id, root)."""
        canonical = str(root.expanduser().resolve())
        existing = self.conn.execute(
            "SELECT * FROM workspace_sources WHERE workspace_id = ? AND root_path = ?",
            (workspace_id, canonical),
        ).fetchone()
        if existing is not None:
            if label is not None and label != existing["label"]:
                self.conn.execute(
                    "UPDATE workspace_sources SET label = ? WHERE id = ?",
                    (label, existing["id"]),
                )
            return self._row_to_source(dict(existing))
        sid = new_id()
        self.conn.execute(
            """
            INSERT INTO workspace_sources(id, workspace_id, root_path, label)
            VALUES (?, ?, ?, ?)
            """,
            (sid, workspace_id, canonical, label),
        )
        return WorkspaceSource(
            id=sid, workspace_id=workspace_id, root_path=canonical,
            label=label, added_at=now_iso(), last_scanned_at=None,
        )

    def list_sources(self, workspace_id: str) -> list[WorkspaceSource]:
        rows = self.conn.execute(
            "SELECT * FROM workspace_sources WHERE workspace_id = ? ORDER BY added_at",
            (workspace_id,),
        ).fetchall()
        return [self._row_to_source(dict(r)) for r in rows]

    def remove_source(self, workspace_id: str, source_id_or_path: str) -> bool:
        """Remove by source-id or root_path. Returns True if a row was deleted."""
        cursor = self.conn.execute(
            "DELETE FROM workspace_sources WHERE workspace_id = ? AND id = ?",
            (workspace_id, source_id_or_path),
        )
        if cursor.rowcount > 0:
            return True
        canonical = str(Path(source_id_or_path).expanduser().resolve())
        cursor = self.conn.execute(
            "DELETE FROM workspace_sources WHERE workspace_id = ? AND root_path = ?",
            (workspace_id, canonical),
        )
        return cursor.rowcount > 0

    def mark_source_scanned(self, source_id: str) -> None:
        self.conn.execute(
            "UPDATE workspace_sources SET last_scanned_at = ? WHERE id = ?",
            (now_iso(), source_id),
        )

    # -----------------------------------------------------------------------
    # Workspace membership (raws ↔ workspace)
    # -----------------------------------------------------------------------

    def add_member(self, workspace_id: str, node_id: str) -> WorkspaceMembership:
        """Record that a raw node belongs to this workspace. Idempotent."""
        self.conn.execute(
            """
            INSERT INTO workspace_membership(workspace_id, node_id)
            VALUES (?, ?)
            ON CONFLICT(workspace_id, node_id) DO NOTHING
            """,
            (workspace_id, node_id),
        )
        return WorkspaceMembership(workspace_id=workspace_id, node_id=node_id)

    def remove_member(self, workspace_id: str, node_id: str) -> bool:
        cursor = self.conn.execute(
            "DELETE FROM workspace_membership WHERE workspace_id = ? AND node_id = ?",
            (workspace_id, node_id),
        )
        return cursor.rowcount > 0

    def member_node_ids(self, workspace_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT node_id FROM workspace_membership WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchall()
        return [r["node_id"] for r in rows]

    # -----------------------------------------------------------------------
    # Project ↔ workspace links
    # -----------------------------------------------------------------------

    def link_project(
        self,
        project_id: str,
        workspace_id: str,
        *,
        role: WorkspaceRole = "reference",
        weight: float = 1.0,
    ) -> ProjectWorkspace:
        """Link a workspace to a project. Idempotent; updates role/weight if changed."""
        ts = now_iso()
        self.conn.execute(
            """
            INSERT INTO project_workspaces(project_id, workspace_id, linked_at, role, weight)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project_id, workspace_id) DO UPDATE SET
                role   = excluded.role,
                weight = excluded.weight
            """,
            (project_id, workspace_id, ts, role, weight),
        )
        return ProjectWorkspace(
            project_id=project_id, workspace_id=workspace_id,
            linked_at=ts, role=role, weight=weight,
        )

    def unlink_project(self, project_id: str, workspace_id: str) -> bool:
        """Remove a project↔workspace link. Returns True if a row was deleted."""
        cursor = self.conn.execute(
            "DELETE FROM project_workspaces WHERE project_id = ? AND workspace_id = ?",
            (project_id, workspace_id),
        )
        return cursor.rowcount > 0

    def linked_workspaces(self, project_id: str) -> list[tuple[Workspace, ProjectWorkspace]]:
        """Return (workspace, link) pairs for a project, ordered by role then slug."""
        rows = self.conn.execute(
            """
            SELECT w.*, pw.linked_at, pw.role, pw.weight, pw.last_relevance_pass_at
            FROM   information_workspaces w
            JOIN   project_workspaces pw ON pw.workspace_id = w.id
            WHERE  pw.project_id = ?
            ORDER  BY CASE pw.role WHEN 'primary' THEN 0 WHEN 'reference' THEN 1 ELSE 2 END,
                       w.slug
            """,
            (project_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            workspace = self._row_to_workspace(d)
            link = ProjectWorkspace(
                project_id=project_id,
                workspace_id=d["id"],
                linked_at=d["linked_at"],
                role=d["role"],
                weight=d["weight"],
                last_relevance_pass_at=d.get("last_relevance_pass_at"),
            )
            result.append((workspace, link))
        return result

    def linked_project_ids(self, workspace_id: str) -> list[str]:
        """Return project_ids for all projects that have linked this workspace."""
        rows = self.conn.execute(
            """
            SELECT project_id FROM project_workspaces
            WHERE workspace_id = ? AND role != 'excluded'
            """,
            (workspace_id,),
        ).fetchall()
        return [r["project_id"] for r in rows]

    def update_relevance_pass_ts(self, project_id: str, workspace_id: str) -> None:
        self.conn.execute(
            """
            UPDATE project_workspaces
            SET last_relevance_pass_at = ?
            WHERE project_id = ? AND workspace_id = ?
            """,
            (now_iso(), project_id, workspace_id),
        )

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    def _row_to_workspace(self, row: sqlite3.Row | dict) -> Workspace:
        d = dict(row)
        return Workspace(
            id=d["id"], slug=d["slug"], name=d["name"],
            description_md=d.get("description_md") or "",
            kind=d.get("kind") or "mixed",
            created_at=d["created_at"], last_active_at=d["last_active_at"],
            last_scanned_at=d.get("last_scanned_at"),
            config=json.loads(d["config"]) if d.get("config") else {},
        )

    def _row_to_source(self, row: dict) -> WorkspaceSource:
        return WorkspaceSource(
            id=row["id"], workspace_id=row["workspace_id"],
            root_path=row["root_path"], label=row.get("label"),
            added_at=row.get("added_at") or now_iso(),
            last_scanned_at=row.get("last_scanned_at"),
        )
