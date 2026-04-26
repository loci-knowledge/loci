"""Edge repository — directed acyclic graph (DAG) writer.

Two edge types, both directed:

    cites         interp → raw    (a locus pointing at its source)
    derives_from  interp → interp (a locus building on another locus)

Invariants enforced here (the SQL CHECK only enforces the type vocabulary):

  - kind direction: src and dst must match `EDGE_DIRECTION[type]`.
  - raw-leaf rule:  raws never have outgoing edges (a consequence of the rules
                    above — there is no edge type with src=raw — but we assert
                    it loudly so callers can't sneak past via a bad `type`).
  - acyclicity:     a derives_from edge that would close a cycle is rejected.
                    Detected via a forward-reachability check from `dst` back
                    to `src` before insert. Cheap for the personal-scale graph
                    sizes loci targets; expensive only on truly degenerate
                    inputs, which the cycle check then blocks.

No symmetric edges, no inverses. The previous `semantic` (symmetric interp↔interp)
and `actual` (raw↔raw) types are gone; use `derives_from` if you need to express
"locus B builds on locus A" directionally, or a pair of cites edges if two
interps simply share a source.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from loci.graph.models import (
    EDGE_DIRECTION,
    Edge,
    EdgeCreator,
    EdgeType,
    new_id,
    now_iso,
)


class EdgeError(ValueError):
    """Raised when an edge violates a topology invariant (direction or cycle)."""


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
        """Outgoing edges (src = node_id), optionally filtered by type."""
        type_list = list(types) if types else None
        if type_list:
            placeholders = ",".join("?" * len(type_list))
            params: tuple = (node_id, *type_list)
            rows = self.conn.execute(
                f"SELECT * FROM edges WHERE src = ? AND type IN ({placeholders})", params
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM edges WHERE src = ?", (node_id,)
            ).fetchall()
        return [self._row_to_edge(r) for r in rows]

    def to_node(self, node_id: str, types: Iterable[EdgeType] | None = None) -> list[Edge]:
        """Incoming edges (dst = node_id), optionally filtered by type."""
        type_list = list(types) if types else None
        if type_list:
            placeholders = ",".join("?" * len(type_list))
            params: tuple = (node_id, *type_list)
            rows = self.conn.execute(
                f"SELECT * FROM edges WHERE dst = ? AND type IN ({placeholders})", params
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM edges WHERE dst = ?", (node_id,)
            ).fetchall()
        return [self._row_to_edge(r) for r in rows]

    def neighbors(self, node_id: str, types: Iterable[EdgeType] | None = None) -> list[str]:
        """dst node ids of outgoing edges. Convenience wrapper."""
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
    ) -> Edge:
        """Insert a directed edge after validating direction and acyclicity.

        Returns the inserted edge, or the existing row if (src, dst, type)
        already exists (idempotent create-or-get).

        Raises `EdgeError` if the edge violates direction (src/dst kind) or
        would close a cycle in the derives_from sub-graph.
        """
        if src == dst:
            raise EdgeError(f"edge cannot be a self-loop: {src}")
        self._check_direction(src, dst, type)
        if type == "derives_from":
            self._check_no_cycle(src, dst)

        from loci.db.connection import transaction
        edge = self._build(src, dst, type, weight, created_by,
                           rationale=rationale, angle=angle)
        with transaction(self.conn):
            return self._insert_or_get(edge)

    def delete(self, edge_id: str) -> None:
        self.conn.execute("DELETE FROM edges WHERE id = ?", (edge_id,))

    def update_weight(self, edge_id: str, new_weight: float) -> None:
        self.conn.execute(
            "UPDATE edges SET weight = ? WHERE id = ?", (new_weight, edge_id)
        )

    # -----------------------------------------------------------------------
    # Topology checks
    # -----------------------------------------------------------------------

    def _check_direction(self, src: str, dst: str, type: EdgeType) -> None:
        """Verify src.kind / dst.kind match EDGE_DIRECTION[type]."""
        expected = EDGE_DIRECTION.get(type)
        if expected is None:
            raise EdgeError(f"unknown edge type: {type}")
        rows = self.conn.execute(
            "SELECT id, kind FROM nodes WHERE id IN (?, ?)", (src, dst),
        ).fetchall()
        kinds = {r["id"]: r["kind"] for r in rows}
        src_kind = kinds.get(src)
        dst_kind = kinds.get(dst)
        if src_kind is None or dst_kind is None:
            raise EdgeError(f"endpoint not found: src={src} dst={dst}")
        want_src, want_dst = expected
        if src_kind != want_src or dst_kind != want_dst:
            raise EdgeError(
                f"{type} requires {want_src}→{want_dst}, "
                f"got {src_kind}→{dst_kind}"
            )

    def _check_no_cycle(self, src: str, dst: str) -> None:
        """Reject a derives_from edge if `dst` already reaches `src`.

        Forward BFS from dst over derives_from edges. Since the existing graph
        is acyclic by construction, this is bounded by the size of the
        connected component of dst.
        """
        seen: set[str] = set()
        frontier: list[str] = [dst]
        while frontier:
            node = frontier.pop()
            if node == src:
                raise EdgeError(
                    f"derives_from {src}→{dst} would close a cycle"
                )
            if node in seen:
                continue
            seen.add(node)
            rows = self.conn.execute(
                "SELECT dst FROM edges WHERE src = ? AND type = 'derives_from'",
                (node,),
            ).fetchall()
            frontier.extend(r["dst"] for r in rows)

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
            rationale=rationale,
            angle=angle,
        )

    def _insert_or_get(self, edge: Edge) -> Edge:
        try:
            self.conn.execute(
                """
                INSERT INTO edges(id, src, dst, type, weight, created_at,
                                   created_by, rationale, angle)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    edge.id, edge.src, edge.dst, edge.type, edge.weight,
                    edge.created_at, edge.created_by,
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
        keys = row.keys()
        return Edge(
            id=row["id"], src=row["src"], dst=row["dst"], type=row["type"],
            weight=row["weight"], created_at=row["created_at"],
            created_by=row["created_by"],
            rationale=row["rationale"] if "rationale" in keys else None,
            angle=row["angle"] if "angle" in keys else None,
        )
