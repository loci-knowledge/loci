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

import json
import sqlite3

from loci.graph.models import Aspect, ResourceAspect, new_id, now_iso

# ---------------------------------------------------------------------------
# Provenance helper (module-level to avoid circular imports)
# ---------------------------------------------------------------------------


def _insert_provenance(
    conn: sqlite3.Connection,
    resource_id: str,
    aspect_id: str,
    action: str,
    source: str,
    confidence: float | None,
    project_id: str | None = None,
    rationale: str | None = None,
    session_hash: str | None = None,
) -> None:
    """Append one row to aspect_provenance (best-effort; silently skipped if table absent)."""
    try:
        conn.execute(
            """
            INSERT INTO aspect_provenance(
                id, project_id, resource_id, aspect_id,
                action, source, confidence, rationale, session_hash, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id(),
                project_id,
                resource_id,
                aspect_id,
                action,
                source,
                confidence,
                rationale,
                session_hash,
                now_iso(),
            ),
        )
    except sqlite3.OperationalError:
        # Table not yet created (migration pending) — degrade gracefully.
        pass


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

    def ensure_aspect(self, label: str, source: str = "user", project_id: str | None = None) -> Aspect:
        """Get or create an aspect by label. Returns the existing row if found.

        `source` is used only when creating: it sets `user_defined` or
        `auto_inferred` flags based on whether the caller is a human or a
        pipeline job.

        When `project_id` is given, the aspect is scoped to that project.
        The UNIQUE constraint is on (label, COALESCE(project_id, '')).

        For backward compat: creates with topic=label, DSL fields=NULL.
        Use `ensure_aspect_proposition` when a structured proposition is available.
        """
        label = label.strip()
        row = self.conn.execute(
            "SELECT * FROM aspect_vocab WHERE label = ? AND project_id IS ?",
            (label, project_id),
        ).fetchone()
        if row is not None:
            aspect = self._row_to_aspect(row)
            # Opportunistically backfill topic if missing (post-migration)
            if row["topic"] is None:
                try:
                    self.conn.execute(
                        "UPDATE aspect_vocab SET topic = ? WHERE id = ?",
                        (label, aspect.id),
                    )
                    aspect = Aspect(**{**aspect.__dict__, "topic": label})
                except Exception:  # noqa: BLE001
                    pass
            return aspect

        aspect_id = new_id()
        user_defined = 1 if source == "user" else 0
        auto_inferred = 1 if source == "inferred" else 0
        ts = now_iso()
        try:
            self.conn.execute(
                """
                INSERT INTO aspect_vocab(id, label, topic, description,
                                         conceptnet_relation_hint,
                                         user_defined, auto_inferred, last_used, created_at,
                                         project_id)
                VALUES (?, ?, ?, NULL, NULL, ?, ?, NULL, ?, ?)
                """,
                (aspect_id, label, label, user_defined, auto_inferred, ts, project_id),
            )
        except Exception:  # noqa: BLE001
            # Column may not exist on very old schemas (migration pending)
            self.conn.execute(
                """
                INSERT INTO aspect_vocab(id, label, description, conceptnet_relation_hint,
                                         user_defined, auto_inferred, last_used, created_at,
                                         project_id)
                VALUES (?, ?, NULL, NULL, ?, ?, NULL, ?, ?)
                """,
                (aspect_id, label, user_defined, auto_inferred, ts, project_id),
            )
        return Aspect(
            id=aspect_id,
            label=label,
            topic=label,
            description=None,
            conceptnet_relation_hint=None,
            user_defined=bool(user_defined),
            auto_inferred=bool(auto_inferred),
            last_used=None,
            created_at=ts,
        )

    def ensure_aspect_proposition(
        self,
        prop: "Proposition",  # noqa: F821 — forward ref
        source: str = "user",
        project_id: str | None = None,
    ) -> Aspect:
        """Get or create an aspect for a structured Proposition.

        The canonical label is the rendered CNL string (e.g.
        "reproducibility as critique critiques frequentist-statistics").
        DSL columns (topic, kind, role, target_aspect_id, modifiers_json)
        are written alongside the label.

        When `prop.role` points to another topic, that target topic must
        already exist in aspect_vocab for `target_aspect_id` to be resolved.
        A target that doesn't exist is silently left NULL (the FK is nullable).
        """
        from loci.graph.aspect_dsl import render  # noqa: PLC0415

        label = render(prop)
        row = self.conn.execute(
            "SELECT * FROM aspect_vocab WHERE label = ? AND project_id IS ?",
            (label, project_id),
        ).fetchone()
        if row is not None:
            return self._row_to_aspect(row)

        target_aspect_id: str | None = None
        if prop.target is not None:
            target_aspect_id = self._resolve_aspect_id_by_topic(prop.target, project_id)
            if target_aspect_id is None:
                # Auto-create the target topic as a flat/degenerate aspect so
                # the role edge can be materialized (target FK must exist).
                target_aspect = self.ensure_aspect(
                    prop.target, source="inferred", project_id=project_id
                )
                target_aspect_id = target_aspect.id

        modifiers_json = json.dumps(prop.modifiers) if prop.modifiers else None
        aspect_id = new_id()
        user_defined = 1 if source == "user" else 0
        auto_inferred = 1 if source == "inferred" else 0
        ts = now_iso()
        try:
            self.conn.execute(
                """
                INSERT INTO aspect_vocab(
                    id, label, topic, kind, role, target_aspect_id, modifiers_json,
                    description, conceptnet_relation_hint,
                    user_defined, auto_inferred, last_used, created_at, project_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL, ?, ?)
                """,
                (
                    aspect_id, label, prop.topic, prop.kind, prop.role,
                    target_aspect_id, modifiers_json,
                    user_defined, auto_inferred, ts, project_id,
                ),
            )
        except Exception:  # noqa: BLE001
            # Fallback: schema pre-DSL migration — write label only
            return self.ensure_aspect(label, source=source, project_id=project_id)

        return Aspect(
            id=aspect_id,
            label=label,
            topic=prop.topic,
            kind=prop.kind,
            role=prop.role,
            target_aspect_id=target_aspect_id,
            modifiers=prop.modifiers,
            description=None,
            conceptnet_relation_hint=None,
            user_defined=bool(user_defined),
            auto_inferred=bool(auto_inferred),
            last_used=None,
            created_at=ts,
        )

    def get_by_label(self, label: str, project_id: str | None = None) -> Aspect | None:
        """Return the aspect for `label`, or None if not in vocab."""
        row = self.conn.execute(
            "SELECT * FROM aspect_vocab WHERE label = ? AND project_id IS ?",
            (label.strip(), project_id),
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

        When `project_id` is given, returns global aspects (project_id IS NULL)
        that have at least one resource in the project, UNION project-local aspects
        (project_id = ?) that have at least one resource in the project.
        When `project_id` is None, returns all aspects.
        """
        if project_id:
            rows = self.conn.execute(
                """
                SELECT DISTINCT av.*
                FROM aspect_vocab av
                JOIN resource_aspects ra ON ra.aspect_id = av.id
                JOIN project_effective_members pm ON pm.node_id = ra.resource_id
                WHERE pm.project_id = ? AND av.project_id IS NULL

                UNION

                SELECT DISTINCT av.*
                FROM aspect_vocab av
                JOIN project_resource_aspects pra ON pra.aspect_id = av.id
                JOIN project_effective_members pm ON pm.node_id = pra.resource_id
                WHERE pm.project_id = ? AND pra.project_id = ?

                ORDER BY label
                """,
                (project_id, project_id, project_id),
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

    def add_provenance_entry(
        self,
        project_id: str | None,
        resource_id: str,
        aspect_id: str,
        action: str,
        source: str,
        confidence: float | None,
        rationale: str | None = None,
        session_hash: str | None = None,
    ) -> None:
        """Append one row to aspect_provenance.

        This is the canonical entry point for recording aspect history.
        Silently skipped if the aspect_provenance table is not yet present
        (migration pending on an older DB).
        """
        _insert_provenance(
            conn=self.conn,
            resource_id=resource_id,
            aspect_id=aspect_id,
            action=action,
            source=source,
            confidence=confidence,
            project_id=project_id,
            rationale=rationale,
            session_hash=session_hash,
        )

    def tag_resource(
        self,
        resource_id: str,
        aspect_labels: list[str],
        source: str,
        confidence: float = 1.0,
        project_id: str | None = None,
    ) -> None:
        """Add aspect tags to a resource.

        Idempotent: if the (resource_id, aspect_id) pair already exists, the
        confidence and source are updated in place. Unknown labels are created
        in the vocab automatically via `ensure_aspect`.

        When `project_id` is given, writes to `project_resource_aspects` instead
        of `resource_aspects` (project-scoped path).

        Each tag operation is also appended to aspect_provenance for audit trail.
        """
        ts = now_iso()
        for label in aspect_labels:
            aspect = self.ensure_aspect(label, source=source)
            if project_id is not None:
                self.conn.execute(
                    """
                    INSERT INTO project_resource_aspects(
                        project_id, resource_id, aspect_id, confidence, source,
                        weight_signals_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                    ON CONFLICT(project_id, resource_id, aspect_id) DO UPDATE SET
                        confidence = excluded.confidence,
                        source = excluded.source,
                        updated_at = excluded.updated_at
                    """,
                    (project_id, resource_id, aspect.id, confidence, source, ts, ts),
                )
            else:
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
            self.add_provenance_entry(
                project_id=project_id,
                resource_id=resource_id,
                aspect_id=aspect.id,
                action="added",
                source=source,
                confidence=confidence,
            )

    def untag_resource(self, resource_id: str, aspect_labels: list[str], project_id: str | None = None) -> None:
        """Remove specific aspect tags from a resource.

        Each removal is appended to aspect_provenance for audit trail.
        """
        if not aspect_labels:
            return
        for label in aspect_labels:
            aspect = self.get_by_label(label)
            if aspect is None:
                continue
            if project_id is not None:
                self.conn.execute(
                    """
                    DELETE FROM project_resource_aspects
                    WHERE project_id = ? AND resource_id = ? AND aspect_id = ?
                    """,
                    (project_id, resource_id, aspect.id),
                )
            else:
                self.conn.execute(
                    "DELETE FROM resource_aspects WHERE resource_id = ? AND aspect_id = ?",
                    (resource_id, aspect.id),
                )
            self.add_provenance_entry(
                project_id=project_id,
                resource_id=resource_id,
                aspect_id=aspect.id,
                action="removed",
                source="user",
                confidence=None,
            )

    def aspects_for(self, resource_id: str, project_id: str | None = None) -> list[ResourceAspect]:
        """All aspects associated with a resource, with confidence and source."""
        if project_id is not None:
            rows = self.conn.execute(
                """
                SELECT pra.resource_id, pra.aspect_id, pra.confidence, pra.source,
                       pra.created_at, av.label
                FROM project_resource_aspects pra
                JOIN aspect_vocab av ON av.id = pra.aspect_id
                WHERE pra.resource_id = ? AND pra.project_id = ?
                ORDER BY pra.confidence DESC, av.label
                """,
                (resource_id, project_id),
            ).fetchall()
        else:
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
                SELECT pra.resource_id
                FROM project_resource_aspects pra
                JOIN project_effective_members pm ON pm.node_id = pra.resource_id
                WHERE pra.aspect_id = ? AND pra.project_id = ? AND pm.project_id = ?
                ORDER BY pra.confidence DESC
                LIMIT ?
                """,
                (aspect.id, project_id, project_id, limit),
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
        Reads from project_resource_aspects first; falls back to resource_aspects
        for projects without migrated data.
        """
        rows = self.conn.execute(
            """
            SELECT av.label, COUNT(pra.resource_id) AS cnt
            FROM project_resource_aspects pra
            JOIN aspect_vocab av ON av.id = pra.aspect_id
            JOIN project_effective_members pm ON pm.node_id = pra.resource_id
            WHERE pra.project_id = ? AND pm.project_id = ?
            GROUP BY av.id, av.label

            UNION ALL

            SELECT av.label, COUNT(ra.resource_id) AS cnt
            FROM resource_aspects ra
            JOIN aspect_vocab av ON av.id = ra.aspect_id
            JOIN project_effective_members pm ON pm.node_id = ra.resource_id
            WHERE pm.project_id = ?
              AND NOT EXISTS (
                SELECT 1 FROM project_resource_aspects pra2
                WHERE pra2.project_id = ? AND pra2.resource_id = ra.resource_id
              )
            GROUP BY av.id, av.label
            """,
            (project_id, project_id, project_id, project_id),
        ).fetchall()

        # Merge counts for the same label across both sources
        counts: dict[str, int] = {}
        for r in rows:
            label = r["label"]
            counts[label] = counts.get(label, 0) + r["cnt"]

        return sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:limit]

    def clear_resource_aspects(self, resource_id: str, project_id: str | None = None) -> None:
        """Remove all aspect associations for a resource (e.g. before re-ingest)."""
        if project_id is not None:
            self.conn.execute(
                "DELETE FROM project_resource_aspects WHERE resource_id = ? AND project_id = ?",
                (resource_id, project_id),
            )
        else:
            self.conn.execute(
                "DELETE FROM resource_aspects WHERE resource_id = ?", (resource_id,)
            )

    def tag_resource_with_propositions(
        self,
        project_id: str,
        resource_id: str,
        aspect_scores: "list[AspectScore]",  # noqa: F821
        source: str,
        confidence_override: float | None = None,
        weight_signals: dict | None = None,
    ) -> None:
        """Write project-scoped aspects from structured AspectScores (v2.2).

        For each AspectScore that carries a Proposition, uses
        `ensure_aspect_proposition` so the DSL columns are populated and role
        edges are materialised. Falls back to `ensure_aspect` for flat labels.

        Gold-protection: skips rows where source='user' already exists.
        """
        from loci.graph.aspect_dsl import render, to_aspect_edges  # noqa: PLC0415
        from loci.graph.aspect_graph import AspectGraphRepository  # noqa: PLC0415

        aspect_graph = AspectGraphRepository(self.conn)
        ts = now_iso()
        wj = json.dumps(weight_signals) if weight_signals else None

        for score in aspect_scores:
            conf = confidence_override if confidence_override is not None else score.confidence
            prop = getattr(score, "proposition", None)

            if prop is not None and not prop.is_flat:
                aspect = self.ensure_aspect_proposition(
                    prop, source=source, project_id=project_id
                )
            else:
                label = getattr(score, "label", render(prop)) if prop else score.label
                aspect = self.ensure_aspect(label, source=source, project_id=project_id)
                prop = None  # flat

            # UPSERT with gold-protection
            self.conn.execute(
                """
                INSERT INTO project_resource_aspects(
                    project_id, resource_id, aspect_id, confidence, source,
                    weight_signals_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, resource_id, aspect_id) DO UPDATE SET
                    confidence = excluded.confidence,
                    source = excluded.source,
                    weight_signals_json = excluded.weight_signals_json,
                    updated_at = excluded.updated_at
                WHERE project_resource_aspects.source != 'user'
                """,
                (project_id, resource_id, aspect.id, conf, source, wj, ts, ts),
            )
            self.touch_last_used(aspect.label)
            rationale = score.rationale or (f"kind={prop.kind} role={prop.role}" if prop else None)
            self.add_provenance_entry(
                project_id=project_id,
                resource_id=resource_id,
                aspect_id=aspect.id,
                action="added",
                source=source,
                confidence=conf,
                rationale=rationale,
            )

            # Materialise role-derived aspect_edges when role is present
            if prop is not None and prop.role is not None and prop.target is not None:
                specs = to_aspect_edges(
                    prop,
                    project_id=project_id,
                    resource_id=resource_id,
                    aspect_id=aspect.id,
                )
                for spec in specs:
                    src_id = self._resolve_aspect_id_by_topic(spec.src_topic, project_id)
                    dst_id = self._resolve_aspect_id_by_topic(spec.dst_topic, project_id)
                    if src_id and dst_id:
                        try:
                            self.conn.execute(
                                """
                                INSERT INTO aspect_edges(
                                    id, src_aspect_id, dst_aspect_id, project_id,
                                    edge_type, weight, computed_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                                ON CONFLICT(src_aspect_id, dst_aspect_id, COALESCE(project_id,''), edge_type)
                                DO UPDATE SET weight = excluded.weight, computed_at = excluded.computed_at
                                """,
                                (spec.deterministic_id, src_id, dst_id, project_id,
                                 spec.edge_type, spec.weight, ts),
                            )
                        except Exception:  # noqa: BLE001
                            pass  # aspect_edges may have different conflict key; degrade

    def tag_resource_project(
        self,
        project_id: str,
        resource_id: str,
        aspect_labels: list[str],
        source: str,
        confidence: float = 1.0,
        weight_signals: dict | None = None,
    ) -> None:
        """Write project-scoped aspect tags with UPSERT, skipping gold (source='user') rows."""
        ts = now_iso()
        weight_signals_json = json.dumps(weight_signals) if weight_signals is not None else None
        for label in aspect_labels:
            aspect = self.ensure_aspect(label, source=source)
            self.conn.execute(
                """
                INSERT INTO project_resource_aspects(
                    project_id, resource_id, aspect_id, confidence, source,
                    weight_signals_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, resource_id, aspect_id) DO UPDATE SET
                    confidence = excluded.confidence,
                    source = excluded.source,
                    weight_signals_json = excluded.weight_signals_json,
                    updated_at = excluded.updated_at
                WHERE project_resource_aspects.source != 'user'
                """,
                (project_id, resource_id, aspect.id, confidence, source,
                 weight_signals_json, ts, ts),
            )
            self.touch_last_used(label)

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    def _row_to_aspect(self, row: sqlite3.Row | dict) -> Aspect:
        d = dict(row)
        modifiers: dict | None = None
        mj = d.get("modifiers_json")
        if mj:
            try:
                modifiers = json.loads(mj)
            except Exception:  # noqa: BLE001
                modifiers = None
        return Aspect(
            id=d["id"],
            label=d["label"],
            description=d.get("description"),
            conceptnet_relation_hint=d.get("conceptnet_relation_hint"),
            user_defined=bool(d.get("user_defined", False)),
            auto_inferred=bool(d.get("auto_inferred", False)),
            last_used=d.get("last_used"),
            created_at=d["created_at"],
            topic=d.get("topic") or d["label"],
            kind=d.get("kind"),
            role=d.get("role"),
            target_aspect_id=d.get("target_aspect_id"),
            modifiers=modifiers,
        )

    def _resolve_aspect_id_by_topic(self, topic: str, project_id: str | None) -> str | None:
        """Return the aspect_id for a topic slug, checking project scope then global."""
        row = self.conn.execute(
            "SELECT id FROM aspect_vocab WHERE topic = ? AND project_id IS ? LIMIT 1",
            (topic, project_id),
        ).fetchone()
        if row is None:
            row = self.conn.execute(
                "SELECT id FROM aspect_vocab WHERE topic = ? AND project_id IS NULL LIMIT 1",
                (topic,),
            ).fetchone()
        return row["id"] if row else None

    def _row_to_resource_aspect(self, row: sqlite3.Row | dict) -> ResourceAspect:
        d = dict(row)
        return ResourceAspect(
            resource_id=d["resource_id"],
            aspect_id=d["aspect_id"],
            confidence=d.get("confidence", 1.0),
            source=d.get("source", "user"),
            created_at=d.get("created_at", now_iso()),
        )
