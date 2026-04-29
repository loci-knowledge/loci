"""Aspect vocabulary and resource-tagging repository.

Aspects are named concepts used to semantically tag raw resources. This module
owns the `aspect_vocab` and `resource_aspects` tables and provides the full
CRUD surface for both.

Design notes:
- Aspect labels are the primary identifier for end-user interactions; ids are
  used internally. `ensure_aspect()` is the canonical entry point for label→id
  resolution with implicit creation.
- `tag_resource` is idempotent: re-tagging an existing (resource, aspect) pair
  updates confidence and source rather than raising a conflict.
- All timestamps use `now_iso()` from models.py to stay consistent with the
  rest of the graph layer.
"""

from __future__ import annotations

import sqlite3

from loci.graph.models import Aspect, ResourceAspect, new_id, now_iso


class AspectRepository:
    """CRUD for aspects (vocabulary) and resource-aspect associations.

    Constructed with an open SQLite connection. Does not own the connection
    lifetime.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -----------------------------------------------------------------------
    # Vocabulary
    # -----------------------------------------------------------------------

    def ensure_aspect(self, label: str, source: str = "user") -> Aspect:
        """Get or create an aspect by label. Returns the existing row if found.

        `source` is used only when creating: it sets `user_defined` or
        `auto_inferred` flags based on whether the caller is a human or a
        pipeline job.
        """
        label = label.strip()
        row = self.conn.execute(
            "SELECT * FROM aspect_vocab WHERE label = ?", (label,)
        ).fetchone()
        if row is not None:
            return self._row_to_aspect(row)

        aspect_id = new_id()
        user_defined = 1 if source == "user" else 0
        auto_inferred = 1 if source == "inferred" else 0
        ts = now_iso()
        self.conn.execute(
            """
            INSERT INTO aspect_vocab(id, label, description, conceptnet_relation_hint,
                                     user_defined, auto_inferred, last_used, created_at)
            VALUES (?, ?, NULL, NULL, ?, ?, NULL, ?)
            """,
            (aspect_id, label, user_defined, auto_inferred, ts),
        )
        return Aspect(
            id=aspect_id,
            label=label,
            description=None,
            conceptnet_relation_hint=None,
            user_defined=bool(user_defined),
            auto_inferred=bool(auto_inferred),
            last_used=None,
            created_at=ts,
        )

    def get_by_label(self, label: str) -> Aspect | None:
        """Return the aspect for `label`, or None if not in vocab."""
        row = self.conn.execute(
            "SELECT * FROM aspect_vocab WHERE label = ?", (label.strip(),)
        ).fetchone()
        return self._row_to_aspect(row) if row else None

    def get_by_id(self, aspect_id: str) -> Aspect | None:
        """Return the aspect for `aspect_id`, or None if not found."""
        row = self.conn.execute(
            "SELECT * FROM aspect_vocab WHERE id = ?", (aspect_id,)
        ).fetchone()
        return self._row_to_aspect(row) if row else None

    def list_vocab(self, project_id: str | None = None) -> list[Aspect]:
        """All known aspect labels, optionally filtered to those used in a project.

        When `project_id` is given, only aspects that have at least one
        associated resource inside the project's effective membership are
        returned.
        """
        if project_id:
            rows = self.conn.execute(
                """
                SELECT DISTINCT av.*
                FROM aspect_vocab av
                JOIN resource_aspects ra ON ra.aspect_id = av.id
                JOIN project_effective_members pm ON pm.node_id = ra.resource_id
                WHERE pm.project_id = ?
                ORDER BY av.label
                """,
                (project_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM aspect_vocab ORDER BY label"
            ).fetchall()
        return [self._row_to_aspect(r) for r in rows]

    def update_vocab(
        self,
        aspect_id: str,
        description: str | None = None,
        relation_hint: str | None = None,
    ) -> None:
        """Update the description and/or conceptnet_relation_hint on an aspect."""
        sets: list[str] = []
        params: list[object] = []
        if description is not None:
            sets.append("description = ?")
            params.append(description)
        if relation_hint is not None:
            sets.append("conceptnet_relation_hint = ?")
            params.append(relation_hint)
        if not sets:
            return
        params.append(aspect_id)
        self.conn.execute(
            f"UPDATE aspect_vocab SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )

    def touch_last_used(self, aspect_label: str) -> None:
        """Update the `last_used` timestamp for the aspect with this label."""
        self.conn.execute(
            "UPDATE aspect_vocab SET last_used = ? WHERE label = ?",
            (now_iso(), aspect_label.strip()),
        )

    def delete_aspect(self, aspect_id: str) -> None:
        """Hard-delete an aspect and all its resource associations."""
        self.conn.execute(
            "DELETE FROM resource_aspects WHERE aspect_id = ?", (aspect_id,)
        )
        self.conn.execute(
            "DELETE FROM aspect_vocab WHERE id = ?", (aspect_id,)
        )

    # -----------------------------------------------------------------------
    # Resource tagging
    # -----------------------------------------------------------------------

    def tag_resource(
        self,
        resource_id: str,
        aspect_labels: list[str],
        source: str,
        confidence: float = 1.0,
    ) -> None:
        """Add aspect tags to a resource.

        Idempotent: if the (resource_id, aspect_id) pair already exists, the
        confidence and source are updated in place. Unknown labels are created
        in the vocab automatically via `ensure_aspect`.
        """
        ts = now_iso()
        for label in aspect_labels:
            aspect = self.ensure_aspect(label, source=source)
            self.conn.execute(
                """
                INSERT INTO resource_aspects(resource_id, aspect_id, confidence, source, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(resource_id, aspect_id) DO UPDATE SET
                    confidence = excluded.confidence,
                    source = excluded.source
                """,
                (resource_id, aspect.id, confidence, source, ts),
            )
            self.touch_last_used(label)

    def untag_resource(self, resource_id: str, aspect_labels: list[str]) -> None:
        """Remove specific aspect tags from a resource."""
        if not aspect_labels:
            return
        for label in aspect_labels:
            aspect = self.get_by_label(label)
            if aspect is None:
                continue
            self.conn.execute(
                "DELETE FROM resource_aspects WHERE resource_id = ? AND aspect_id = ?",
                (resource_id, aspect.id),
            )

    def aspects_for(self, resource_id: str) -> list[ResourceAspect]:
        """All aspects associated with a resource, with confidence and source."""
        rows = self.conn.execute(
            """
            SELECT ra.*, av.label
            FROM resource_aspects ra
            JOIN aspect_vocab av ON av.id = ra.aspect_id
            WHERE ra.resource_id = ?
            ORDER BY ra.confidence DESC, av.label
            """,
            (resource_id,),
        ).fetchall()
        return [self._row_to_resource_aspect(r) for r in rows]

    def resources_for_aspect(
        self,
        aspect_label: str,
        project_id: str | None = None,
        limit: int = 50,
    ) -> list[str]:
        """Resource IDs tagged with this aspect, optionally filtered by project."""
        aspect = self.get_by_label(aspect_label)
        if aspect is None:
            return []
        if project_id:
            rows = self.conn.execute(
                """
                SELECT ra.resource_id
                FROM resource_aspects ra
                JOIN project_effective_members pm ON pm.node_id = ra.resource_id
                WHERE ra.aspect_id = ? AND pm.project_id = ?
                ORDER BY ra.confidence DESC
                LIMIT ?
                """,
                (aspect.id, project_id, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT resource_id FROM resource_aspects
                WHERE aspect_id = ?
                ORDER BY confidence DESC
                LIMIT ?
                """,
                (aspect.id, limit),
            ).fetchall()
        return [r["resource_id"] for r in rows]

    def top_aspects(self, project_id: str, limit: int = 20) -> list[tuple[str, int]]:
        """Top aspects by resource count for a project.

        Returns a list of (label, count) pairs sorted by count descending.
        Only counts resources in the project's effective membership.
        """
        rows = self.conn.execute(
            """
            SELECT av.label, COUNT(ra.resource_id) AS cnt
            FROM resource_aspects ra
            JOIN aspect_vocab av ON av.id = ra.aspect_id
            JOIN project_effective_members pm ON pm.node_id = ra.resource_id
            WHERE pm.project_id = ?
            GROUP BY av.id, av.label
            ORDER BY cnt DESC, av.label
            LIMIT ?
            """,
            (project_id, limit),
        ).fetchall()
        return [(r["label"], r["cnt"]) for r in rows]

    def clear_resource_aspects(self, resource_id: str) -> None:
        """Remove all aspect associations for a resource (e.g. before re-ingest)."""
        self.conn.execute(
            "DELETE FROM resource_aspects WHERE resource_id = ?", (resource_id,)
        )

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    def _row_to_aspect(self, row: sqlite3.Row | dict) -> Aspect:
        d = dict(row)
        return Aspect(
            id=d["id"],
            label=d["label"],
            description=d.get("description"),
            conceptnet_relation_hint=d.get("conceptnet_relation_hint"),
            user_defined=bool(d.get("user_defined", False)),
            auto_inferred=bool(d.get("auto_inferred", False)),
            last_used=d.get("last_used"),
            created_at=d["created_at"],
        )

    def _row_to_resource_aspect(self, row: sqlite3.Row | dict) -> ResourceAspect:
        d = dict(row)
        return ResourceAspect(
            resource_id=d["resource_id"],
            aspect_id=d["aspect_id"],
            confidence=d.get("confidence", 1.0),
            source=d.get("source", "user"),
            created_at=d.get("created_at", now_iso()),
        )
