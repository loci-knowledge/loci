"""Aspect-graph repository.

Manages the `aspect_edges` table: typed directed edges between entries in
`aspect_vocab`. These edges support:

- Hierarchy climbing: walk_hierarchy("transformer") → ["deep-learning"]
- Opposite / antonym lookup: opposites("supervised") → ["unsupervised"]
- PMI co-occurrence (written by refresh_project_edges job)
- Semantic similarity edges (written by embed_aspects job)
"""

from __future__ import annotations

import logging
import sqlite3
from collections import deque

from loci.graph.models import new_id, now_iso

log = logging.getLogger(__name__)

# Supported edge types — kept in sync with the SQL CHECK constraint.
EDGE_TYPES = frozenset(
    {"parent_of", "related_to", "opposite_of", "alias_of", "co_aspect_pmi", "semantic_sim"}
)


class AspectGraphRepository:
    """CRUD and traversal for aspect-to-aspect edges.

    Constructed with an open SQLite connection. Does not own the connection
    lifetime.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add_edge(
        self,
        src_aspect_id: str,
        dst_aspect_id: str,
        edge_type: str,
        weight: float = 1.0,
        project_id: str | None = None,
    ) -> None:
        """Upsert an aspect-to-aspect edge (idempotent on triple).

        The triple (src_aspect_id, dst_aspect_id, edge_type, project_id) is
        unique. A second call with the same triple updates the weight and
        computed_at timestamp.
        """
        if edge_type not in EDGE_TYPES:
            raise ValueError(
                f"Unknown edge_type {edge_type!r}. Must be one of {sorted(EDGE_TYPES)}"
            )

        ts = now_iso()

        # Try INSERT; if the unique triple already exists, UPDATE weight.
        existing = self.conn.execute(
            """
            SELECT id FROM aspect_edges
            WHERE src_aspect_id = ?
              AND dst_aspect_id = ?
              AND edge_type = ?
              AND COALESCE(project_id, '') = COALESCE(?, '')
            """,
            (src_aspect_id, dst_aspect_id, edge_type, project_id),
        ).fetchone()

        if existing is not None:
            self.conn.execute(
                """
                UPDATE aspect_edges
                SET weight = ?, computed_at = ?
                WHERE id = ?
                """,
                (weight, ts, existing["id"]),
            )
        else:
            self.conn.execute(
                """
                INSERT INTO aspect_edges(
                    id, src_aspect_id, dst_aspect_id, project_id,
                    edge_type, weight, computed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (new_id(), src_aspect_id, dst_aspect_id, project_id,
                 edge_type, weight, ts),
            )

    def walk_hierarchy(
        self,
        aspect_id: str,
        edge_types: tuple[str, ...] = ("parent_of", "alias_of"),
        depth: int = 2,
        project_id: str | None = None,
    ) -> list[str]:
        """Return aspect_ids reachable from *aspect_id* via *edge_types* (BFS).

        Traversal is depth-limited.

        Edge direction convention:
        - ``parent_of``: edge goes ``parent → child`` (e.g. "deep-learning
          parent_of transformer"). To climb the hierarchy *up* from a child
          node we follow the **reverse** direction (find edges where
          ``dst_aspect_id = current`` AND ``edge_type = 'parent_of'``).
        - ``alias_of``: bidirectional — both forward and reverse edges are
          followed.
        - Other edge types: forward only (src → dst).

        Filter: edges where project_id = *project_id* OR project_id IS NULL
        are included.

        Returns unique IDs excluding the start node.
        """
        visited: set[str] = {aspect_id}
        queue: deque[tuple[str, int]] = deque([(aspect_id, 0)])
        result: list[str] = []

        # edge types that we follow in forward direction (src → dst)
        forward_types = tuple(et for et in edge_types if et not in ("parent_of", "alias_of"))
        # edge types that we follow in reverse direction (dst → src) for upward traversal
        reverse_types_up = tuple(et for et in edge_types if et == "parent_of")

        while queue:
            current_id, current_depth = queue.popleft()
            if current_depth >= depth:
                continue

            # Forward edges for non-parent_of, non-alias_of types
            if forward_types:
                et_placeholders_fwd = ",".join("?" * len(forward_types))
                rows = self.conn.execute(
                    f"""
                    SELECT dst_aspect_id AS neighbor
                    FROM aspect_edges
                    WHERE src_aspect_id = ?
                      AND edge_type IN ({et_placeholders_fwd})
                      AND (project_id IS NULL OR project_id IS ?)
                    """,
                    (current_id, *forward_types, project_id),
                ).fetchall()
                for row in rows:
                    nid = row["neighbor"]
                    if nid not in visited:
                        visited.add(nid)
                        result.append(nid)
                        queue.append((nid, current_depth + 1))

            # Reverse edges for parent_of: climb upward (child → parent)
            if reverse_types_up:
                up_rows = self.conn.execute(
                    """
                    SELECT src_aspect_id AS neighbor
                    FROM aspect_edges
                    WHERE dst_aspect_id = ?
                      AND edge_type = 'parent_of'
                      AND (project_id IS NULL OR project_id IS ?)
                    """,
                    (current_id, project_id),
                ).fetchall()
                for row in up_rows:
                    nid = row["neighbor"]
                    if nid not in visited:
                        visited.add(nid)
                        result.append(nid)
                        queue.append((nid, current_depth + 1))

            # alias_of: both directions (symmetric)
            if "alias_of" in edge_types:
                for direction_col, where_col in [
                    ("dst_aspect_id", "src_aspect_id"),
                    ("src_aspect_id", "dst_aspect_id"),
                ]:
                    alias_rows = self.conn.execute(
                        f"""
                        SELECT {direction_col} AS neighbor
                        FROM aspect_edges
                        WHERE {where_col} = ?
                          AND edge_type = 'alias_of'
                          AND (project_id IS NULL OR project_id IS ?)
                        """,
                        (current_id, project_id),
                    ).fetchall()
                    for row in alias_rows:
                        nid = row["neighbor"]
                        if nid not in visited:
                            visited.add(nid)
                            result.append(nid)
                            queue.append((nid, current_depth + 1))

        return result

    def opposites(
        self,
        aspect_id: str,
        project_id: str | None = None,
    ) -> list[str]:
        """Return aspect_ids connected via *opposite_of* edges (either direction).

        Filter: edges where project_id = *project_id* OR project_id IS NULL.
        """
        forward = self.conn.execute(
            """
            SELECT dst_aspect_id AS neighbor
            FROM aspect_edges
            WHERE src_aspect_id = ?
              AND edge_type = 'opposite_of'
              AND (project_id IS NULL OR project_id IS ?)
            """,
            (aspect_id, project_id),
        ).fetchall()

        reverse = self.conn.execute(
            """
            SELECT src_aspect_id AS neighbor
            FROM aspect_edges
            WHERE dst_aspect_id = ?
              AND edge_type = 'opposite_of'
              AND (project_id IS NULL OR project_id IS ?)
            """,
            (aspect_id, project_id),
        ).fetchall()

        seen: set[str] = set()
        result: list[str] = []
        for row in (*forward, *reverse):
            nid = row["neighbor"]
            if nid not in seen and nid != aspect_id:
                seen.add(nid)
                result.append(nid)
        return result

    def delete_role_edges_for(
        self,
        project_id: str,
        resource_id: str,
        aspect_id: str,
    ) -> int:
        """Delete all role-derived aspect_edges for a (project, resource, aspect) triple.

        Role edges are identified by their deterministic ID prefix `role|…`.
        Call before re-materialising edges after a proposition edit.
        Returns the number of rows deleted.
        """
        from loci.graph.aspect_dsl import role_edge_det_id, ROLES  # noqa: PLC0415

        det_ids = [
            role_edge_det_id(project_id, resource_id, aspect_id, r)
            for r in ROLES
        ]
        placeholders = ",".join("?" * len(det_ids))
        cursor = self.conn.execute(
            f"DELETE FROM aspect_edges WHERE id IN ({placeholders})",
            det_ids,
        )
        return cursor.rowcount
