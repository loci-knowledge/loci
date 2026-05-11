"""ProjectInterpretationRepository — per-project LLM narrative interpretations."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from loci.graph.models import ProjectInterpretation


class ProjectInterpretationRepository:
    """CRUD for project_interpretations.

    Constructed with an open SQLite connection. Does not own the connection
    lifetime.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, project_id: str, resource_id: str) -> ProjectInterpretation | None:
        """Fetch existing interpretation, or None."""
        row = self.conn.execute(
            """
            SELECT * FROM project_interpretations
            WHERE project_id = ? AND resource_id = ?
            """,
            (project_id, resource_id),
        ).fetchone()
        return self._row_to_interp(row) if row else None

    def upsert(self, interp: ProjectInterpretation) -> None:
        """INSERT OR REPLACE into project_interpretations."""
        self.conn.execute(
            """
            INSERT OR REPLACE INTO project_interpretations(
                project_id, resource_id, summary_md, stance,
                inputs_hash, model_id, generated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interp.project_id,
                interp.resource_id,
                interp.summary_md,
                interp.stance,
                interp.inputs_hash,
                interp.model_id,
                interp.generated_at,
            ),
        )

    def list_for_project(
        self,
        project_id: str,
        limit: int = 50,
        order_by_staleness: bool = False,
    ) -> list[ProjectInterpretation]:
        """List interpretations. When order_by_staleness=True, oldest generated_at first."""
        order = "generated_at ASC" if order_by_staleness else "generated_at DESC"
        rows = self.conn.execute(
            f"""
            SELECT * FROM project_interpretations
            WHERE project_id = ?
            ORDER BY {order}
            LIMIT ?
            """,
            (project_id, limit),
        ).fetchall()
        return [self._row_to_interp(r) for r in rows]

    def delete(self, project_id: str, resource_id: str) -> None:
        """Hard-delete one interpretation row."""
        self.conn.execute(
            """
            DELETE FROM project_interpretations
            WHERE project_id = ? AND resource_id = ?
            """,
            (project_id, resource_id),
        )

    def delete_for_project(self, project_id: str) -> None:
        """Delete all interpretations for a project (on workspace unlink)."""
        self.conn.execute(
            "DELETE FROM project_interpretations WHERE project_id = ?",
            (project_id,),
        )

    def compute_inputs_hash(
        self,
        content_hash: str,
        profile_md: str,
        top_aspect_labels: list[str],
        recent_queries: list[str],
    ) -> str:
        """SHA-256 of deterministic inputs. If unchanged, skip LLM re-inference."""
        payload = json.dumps(
            {
                "content_hash": content_hash,
                "profile_md": profile_md,
                "top_aspect_labels": sorted(top_aspect_labels),
                "recent_queries": recent_queries,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    def _row_to_interp(self, row: sqlite3.Row | dict) -> ProjectInterpretation:
        d = dict(row)
        return ProjectInterpretation(
            project_id=d["project_id"],
            resource_id=d["resource_id"],
            summary_md=d.get("summary_md", ""),
            stance=d.get("stance"),
            inputs_hash=d.get("inputs_hash", ""),
            model_id=d.get("model_id"),
            generated_at=d.get("generated_at", ""),
        )
