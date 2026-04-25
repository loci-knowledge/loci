"""Source repository — registered scan roots per project.

PLAN.md commits to a "memory space" that includes "Zotero PDFs, Obsidian
notes, codebases, web pages, transcripts" — files spread across multiple
locations on the user's filesystem. The `project_sources` table records
each registered root so `loci scan <project>` (no path) can walk all of
them in one pass.

We persist root paths as their `Path.expanduser().resolve()` form so a
relative path or `~/papers/` becomes the canonical absolute path. This makes
the UNIQUE(project_id, root_path) constraint meaningful and keeps "did the
user already register this?" lookups O(1).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from loci.graph.models import new_id, now_iso


@dataclass
class ProjectSource:
    id: str
    project_id: str
    root_path: str
    label: str | None
    added_at: str
    last_scanned_at: str | None


class SourceRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add(self, project_id: str, root: Path, *, label: str | None = None) -> ProjectSource:
        """Register a directory (or file) as a scan root for the project.

        Idempotent: if the same (project_id, root) is already registered, the
        existing row is returned with its label updated to the new value (if
        provided).
        """
        canonical = str(root.expanduser().resolve())
        existing = self.conn.execute(
            "SELECT * FROM project_sources WHERE project_id = ? AND root_path = ?",
            (project_id, canonical),
        ).fetchone()
        if existing is not None:
            if label is not None and label != existing["label"]:
                self.conn.execute(
                    "UPDATE project_sources SET label = ? WHERE id = ?",
                    (label, existing["id"]),
                )
                existing = dict(existing)
                existing["label"] = label
            return self._row_to_source(dict(existing))
        sid = new_id()
        self.conn.execute(
            """
            INSERT INTO project_sources(id, project_id, root_path, label)
            VALUES (?, ?, ?, ?)
            """,
            (sid, project_id, canonical, label),
        )
        return ProjectSource(
            id=sid, project_id=project_id, root_path=canonical, label=label,
            added_at=now_iso(), last_scanned_at=None,
        )

    def list(self, project_id: str) -> list[ProjectSource]:
        rows = self.conn.execute(
            """
            SELECT * FROM project_sources
            WHERE project_id = ?
            ORDER BY added_at
            """,
            (project_id,),
        ).fetchall()
        return [self._row_to_source(dict(r)) for r in rows]

    def remove(self, project_id: str, source_id_or_path: str) -> bool:
        """Remove by source-id OR by root_path. Returns True if a row was deleted."""
        # Try id first, then fall back to a path-equality lookup.
        cursor = self.conn.execute(
            "DELETE FROM project_sources WHERE project_id = ? AND id = ?",
            (project_id, source_id_or_path),
        )
        if cursor.rowcount > 0:
            return True
        canonical = str(Path(source_id_or_path).expanduser().resolve())
        cursor = self.conn.execute(
            "DELETE FROM project_sources WHERE project_id = ? AND root_path = ?",
            (project_id, canonical),
        )
        return cursor.rowcount > 0

    def mark_scanned(self, source_id: str) -> None:
        self.conn.execute(
            "UPDATE project_sources SET last_scanned_at = ? WHERE id = ?",
            (now_iso(), source_id),
        )

    def _row_to_source(self, row: dict) -> ProjectSource:
        return ProjectSource(
            id=row["id"], project_id=row["project_id"],
            root_path=row["root_path"], label=row.get("label"),
            added_at=row.get("added_at") or now_iso(),
            last_scanned_at=row.get("last_scanned_at"),
        )
