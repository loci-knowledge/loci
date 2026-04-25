"""Project + ProjectMembership repositories.

A project is a *view* over the global graph (PLAN.md §Projects): a profile,
a config blob, and a set of memberships pointing at nodes. Projects don't own
nodes; nodes can participate in many projects. The `pinned` role marks
touchstones for a project — boosted in retrieval, surfaced in summaries.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

from loci.graph.models import Project, ProjectMembership, Role, now_iso


class ProjectRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -----------------------------------------------------------------------
    # Project CRUD
    # -----------------------------------------------------------------------

    def get(self, project_id: str) -> Project | None:
        row = self.conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        return self._row_to_project(row) if row else None

    def get_by_slug(self, slug: str) -> Project | None:
        row = self.conn.execute(
            "SELECT * FROM projects WHERE slug = ?", (slug,)
        ).fetchone()
        return self._row_to_project(row) if row else None

    def list(self) -> list[Project]:
        rows = self.conn.execute(
            "SELECT * FROM projects ORDER BY last_active_at DESC"
        ).fetchall()
        return [self._row_to_project(r) for r in rows]

    def create(self, project: Project) -> Project:
        self.conn.execute(
            """
            INSERT INTO projects(id, slug, name, profile_md, created_at,
                                  last_active_at, config)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                project.id, project.slug, project.name, project.profile_md,
                project.created_at, project.last_active_at,
                json.dumps(project.config),
            ),
        )
        return project

    def update_profile(self, project_id: str, profile_md: str) -> None:
        self.conn.execute(
            "UPDATE projects SET profile_md = ?, last_active_at = ? WHERE id = ?",
            (profile_md, now_iso(), project_id),
        )

    def touch(self, project_id: str) -> None:
        """Bump last_active_at — called on any read or write into the project."""
        self.conn.execute(
            "UPDATE projects SET last_active_at = ? WHERE id = ?",
            (now_iso(), project_id),
        )

    # -----------------------------------------------------------------------
    # Membership
    # -----------------------------------------------------------------------

    def add_member(
        self, project_id: str, node_id: str,
        role: Role = "included", added_by: str = "user",
    ) -> ProjectMembership:
        """Add (or update) a project membership.

        Idempotent: if the row exists with the same role, no-op. If the role
        differs, update it. Why not raise on conflict? The most common caller
        is the ingest pipeline, which re-runs over the same files.
        """
        self.conn.execute(
            """
            INSERT INTO project_membership(project_id, node_id, role, added_at, added_by)
            VALUES (?,?,?,?,?)
            ON CONFLICT(project_id, node_id) DO UPDATE SET
                role = excluded.role,
                added_at = excluded.added_at,
                added_by = excluded.added_by
            """,
            (project_id, node_id, role, now_iso(), added_by),
        )
        return ProjectMembership(
            project_id=project_id, node_id=node_id, role=role, added_by=added_by,
        )

    def members(
        self, project_id: str,
        roles: Iterable[Role] | None = None,
    ) -> list[str]:
        """Return node_ids in this project, optionally filtered by role."""
        if roles:
            roles = list(roles)
            placeholders = ",".join("?" * len(roles))
            rows = self.conn.execute(
                f"""SELECT node_id FROM project_membership
                    WHERE project_id = ? AND role IN ({placeholders})""",
                (project_id, *roles),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT node_id FROM project_membership
                   WHERE project_id = ? AND role != 'excluded'""",
                (project_id,),
            ).fetchall()
        return [r["node_id"] for r in rows]

    def is_member(self, project_id: str, node_id: str) -> bool:
        row = self.conn.execute(
            """SELECT 1 FROM project_membership
               WHERE project_id = ? AND node_id = ? AND role != 'excluded'""",
            (project_id, node_id),
        ).fetchone()
        return row is not None

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    def _row_to_project(self, row: sqlite3.Row) -> Project:
        return Project(
            id=row["id"], slug=row["slug"], name=row["name"],
            profile_md=row["profile_md"], created_at=row["created_at"],
            last_active_at=row["last_active_at"],
            config=json.loads(row["config"]) if row["config"] else {},
        )
