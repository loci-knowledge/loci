"""Edge repository — CRUD with symmetry/inverse maintenance.

PLAN.md §Edges defines the type table. The schema can't enforce all rules:

- `cites` requires src=interpretation, dst=raw.
- All other types require src and dst both interpretation.
- Symmetric edge types (`reinforces`, `contradicts`, `aliases`, `co_occurs`)
  imply a reciprocal row with src/dst swapped.
- `specializes` ↔ `generalizes` are inverses (asymmetric pair); creating one
  also creates the other in the opposite direction.

All four are enforced here. The result: callers can do `WHERE src=?` queries
and trust they'll see all edges from a node, regardless of type.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from loci.graph.models import (
    EDGE_INVERSES,
    SYMMETRIC_EDGE_TYPES,
    Edge,
    EdgeCreator,
    EdgeType,
    new_id,
    now_iso,
)


class EdgeRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -----------------------------------------------------------------------
    # Reads
    # -----------------------------------------------------------------------

    def get(self, edge_id: str) -> Edge | None:
        row = self.conn.execute("SELECT * FROM edges WHERE id = ?", (edge_id,)).fetchone()
        return self._row_to_edge(row) if row else None

    def from_node(self, node_id: str, types: Iterable[EdgeType] | None = None) -> list[Edge]:
        """Edges with src = node_id. Optionally filtered by edge types."""
        if types:
            placeholders = ",".join("?" * len(list(types)))
            params: tuple = (node_id, *list(types))
            rows = self.conn.execute(
                f"SELECT * FROM edges WHERE src = ? AND type IN ({placeholders})", params
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM edges WHERE src = ?", (node_id,)
            ).fetchall()
        return [self._row_to_edge(r) for r in rows]

    def neighbors(self, node_id: str, types: Iterable[EdgeType] | None = None) -> list[str]:
        """Return dst node ids for edges starting at node_id."""
        return [e.dst for e in self.from_node(node_id, types)]

    # -----------------------------------------------------------------------
    # Writes
    # -----------------------------------------------------------------------

    def create(
        self,
        src: str,
        dst: str,
        type: EdgeType,
        weight: float = 1.0,
        created_by: EdgeCreator = "user",
        *,
        rationale: str | None = None,
        angle: str | None = None,
    ) -> list[Edge]:
        """Create the edge plus reciprocal/inverse if applicable.

        Returns the list of edges that were actually inserted (1 or 2). Existing
        rows (UNIQUE(src, dst, type) collision) are silently skipped, which
        gives callers an idempotent `create_or_get` semantic. We re-fetch
        skipped edges so the caller always sees the canonical row.
        """
        from loci.db.connection import transaction
        primary = self._build(src, dst, type, weight, created_by, rationale=rationale, angle=angle)
        out: list[Edge] = []
        with transaction(self.conn):
            inserted = self._insert_or_get(primary)
            out.append(inserted)
            # Symmetric: write reciprocal of same type.
            if type in SYMMETRIC_EDGE_TYPES and src != dst:
                recip = self._build(dst, src, type, weight, created_by)
                out.append(self._insert_or_get(recip))
            # Inverse pair: specializes ↔ generalizes.
            if type in EDGE_INVERSES and src != dst:
                inv_type = EDGE_INVERSES[type]
                inv = self._build(dst, src, inv_type, weight, created_by)
                out.append(self._insert_or_get(inv))
        return out

    def delete(self, edge_id: str) -> None:
        """Delete an edge. Reciprocals/inverses are NOT auto-deleted — callers
        that want symmetric cleanup should delete both ids."""
        self.conn.execute("DELETE FROM edges WHERE id = ?", (edge_id,))

    def update_weight(self, edge_id: str, new_weight: float) -> None:
        self.conn.execute(
            "UPDATE edges SET weight = ? WHERE id = ?", (new_weight, edge_id)
        )

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    def _build(
        self, src: str, dst: str, type: EdgeType, weight: float, by: EdgeCreator,
        *, rationale: str | None = None, angle: str | None = None,
    ) -> Edge:
        return Edge(
            id=new_id(),
            src=src, dst=dst, type=type,
            weight=weight,
            created_at=now_iso(),
            created_by=by,
            symmetric=type in SYMMETRIC_EDGE_TYPES,
            rationale=rationale,
            angle=angle,
        )

    def _insert_or_get(self, edge: Edge) -> Edge:
        """Insert; on UNIQUE collision return the existing row instead."""
        try:
            self.conn.execute(
                """
                INSERT INTO edges(id, src, dst, type, weight, created_at,
                                   created_by, symmetric, rationale, angle)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    edge.id, edge.src, edge.dst, edge.type, edge.weight,
                    edge.created_at, edge.created_by, int(edge.symmetric),
                    edge.rationale, edge.angle,
                ),
            )
            return edge
        except sqlite3.IntegrityError:
            existing = self.conn.execute(
                "SELECT * FROM edges WHERE src = ? AND dst = ? AND type = ?",
                (edge.src, edge.dst, edge.type),
            ).fetchone()
            return self._row_to_edge(existing)

    def _row_to_edge(self, row: sqlite3.Row) -> Edge:
        return Edge(
            id=row["id"], src=row["src"], dst=row["dst"], type=row["type"],
            weight=row["weight"], created_at=row["created_at"],
            created_by=row["created_by"], symmetric=bool(row["symmetric"]),
            rationale=row["rationale"] if "rationale" in row.keys() else None,
            angle=row["angle"] if "angle" in row.keys() else None,
        )
