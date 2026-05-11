"""Concept edge repository.

Manages typed directed edges between raw resources in the concept graph.
These replace the old interpretation-node DAG (cites / derives_from edges).

Edge types come from two namespaces:
- Structural: cites, wikilink, co_aspect, co_folder, custom
- Semantic (ConceptNet hints): IsA, UsedFor, PartOf, etc.

See `conceptnet_types.py` for the full type list and descriptions.

Design notes:
- `add_edge` is idempotent on (src_id, dst_id, edge_type): re-adding an
  existing edge updates weight and metadata rather than inserting a duplicate.
- `neighbors()` does a BFS / iterative depth-limited walk within SQLite, so
  depth > 1 on a large graph can be slow. For production use, limit to depth 1
  and do multi-hop in Python if needed.
"""

from __future__ import annotations

import json
import sqlite3

from loci.graph.models import ConceptEdge, new_id, now_iso


class ConceptEdgeRepository:
    """CRUD for concept_edges.

    Constructed with an open SQLite connection. Does not own the connection
    lifetime.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -----------------------------------------------------------------------
    # Writes
    # -----------------------------------------------------------------------

    def add_edge(
        self,
        src_id: str,
        dst_id: str,
        edge_type: str,
        relation_hint: str | None = None,
        weight: float = 1.0,
        metadata: dict | None = None,
        project_id: str | None = None,
    ) -> ConceptEdge:
        """Add a typed edge between two resources.

        Idempotent on (src_id, dst_id, edge_type, COALESCE(project_id, '')): if
        the tuple already exists, weight and metadata are updated in place.
        """
        ts = now_iso()
        metadata_json = json.dumps(metadata) if metadata is not None else None

        # Check for existing edge on (src, dst, type, project_id)
        existing = self.conn.execute(
            """
            SELECT * FROM concept_edges
            WHERE src_id = ? AND dst_id = ? AND edge_type = ? AND project_id IS ?
            """,
            (src_id, dst_id, edge_type, project_id),
        ).fetchone()

        if existing is not None:
            self.conn.execute(
                """
                UPDATE concept_edges
                SET weight = ?, relation_hint = ?, metadata = ?
                WHERE src_id = ? AND dst_id = ? AND edge_type = ? AND project_id IS ?
                """,
                (weight, relation_hint, metadata_json, src_id, dst_id, edge_type, project_id),
            )
            return ConceptEdge(
                id=existing["id"],
                src_id=src_id,
                dst_id=dst_id,
                edge_type=edge_type,
                relation_hint=relation_hint,
                weight=weight,
                metadata=metadata,
                created_at=existing["created_at"],
                project_id=project_id,
            )

        edge_id = new_id()
        self.conn.execute(
            """
            INSERT INTO concept_edges(id, src_id, dst_id, edge_type,
                                      relation_hint, weight, metadata, created_at, project_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (edge_id, src_id, dst_id, edge_type, relation_hint, weight, metadata_json, ts, project_id),
        )
        return ConceptEdge(
            id=edge_id,
            src_id=src_id,
            dst_id=dst_id,
            edge_type=edge_type,
            relation_hint=relation_hint,
            weight=weight,
            metadata=metadata,
            created_at=ts,
            project_id=project_id,
        )

    def delete_edge(self, edge_id: str) -> bool:
        """Delete a specific edge by id. Returns True if a row was deleted."""
        cursor = self.conn.execute(
            "DELETE FROM concept_edges WHERE id = ?", (edge_id,)
        )
        return cursor.rowcount > 0

    def delete_edges_for(self, resource_id: str, project_id: str | None = None) -> None:
        """Delete all edges where src_id or dst_id equals resource_id.

        When `project_id` is given, only project-scoped edges are deleted.
        """
        if project_id is not None:
            self.conn.execute(
                """
                DELETE FROM concept_edges
                WHERE (src_id = ? OR dst_id = ?) AND project_id IS ?
                """,
                (resource_id, resource_id, project_id),
            )
        else:
            self.conn.execute(
                "DELETE FROM concept_edges WHERE src_id = ? OR dst_id = ?",
                (resource_id, resource_id),
            )

    # -----------------------------------------------------------------------
    # Reads
    # -----------------------------------------------------------------------

    def get(self, edge_id: str) -> ConceptEdge | None:
        """Fetch a single edge by id."""
        row = self.conn.execute(
            "SELECT * FROM concept_edges WHERE id = ?", (edge_id,)
        ).fetchone()
        return self._row_to_edge(row) if row else None

    def edges_from(
        self,
        src_id: str,
        edge_types: list[str] | None = None,
        project_id: str | None = None,
    ) -> list[ConceptEdge]:
        """All edges from a resource, optionally filtered by edge_type and project.

        When `project_id` is given, returns edges where project_id matches OR is NULL.
        """
        project_filter = "AND (project_id = ? OR project_id IS NULL)" if project_id is not None else ""
        project_params = (project_id,) if project_id is not None else ()

        if edge_types:
            placeholders = ",".join("?" * len(edge_types))
            rows = self.conn.execute(
                f"""
                SELECT * FROM concept_edges
                WHERE src_id = ? AND edge_type IN ({placeholders}) {project_filter}
                ORDER BY weight DESC, created_at
                """,
                (src_id, *edge_types, *project_params),
            ).fetchall()
        else:
            rows = self.conn.execute(
                f"""
                SELECT * FROM concept_edges
                WHERE src_id = ? {project_filter}
                ORDER BY weight DESC, created_at
                """,
                (src_id, *project_params),
            ).fetchall()
        return [self._row_to_edge(r) for r in rows]

    def edges_to(
        self,
        dst_id: str,
        edge_types: list[str] | None = None,
        project_id: str | None = None,
    ) -> list[ConceptEdge]:
        """All edges pointing to a resource, optionally filtered by edge_type and project.

        When `project_id` is given, returns edges where project_id matches OR is NULL.
        """
        project_filter = "AND (project_id = ? OR project_id IS NULL)" if project_id is not None else ""
        project_params = (project_id,) if project_id is not None else ()

        if edge_types:
            placeholders = ",".join("?" * len(edge_types))
            rows = self.conn.execute(
                f"""
                SELECT * FROM concept_edges
                WHERE dst_id = ? AND edge_type IN ({placeholders}) {project_filter}
                ORDER BY weight DESC, created_at
                """,
                (dst_id, *edge_types, *project_params),
            ).fetchall()
        else:
            rows = self.conn.execute(
                f"""
                SELECT * FROM concept_edges
                WHERE dst_id = ? {project_filter}
                ORDER BY weight DESC, created_at
                """,
                (dst_id, *project_params),
            ).fetchall()
        return [self._row_to_edge(r) for r in rows]

    def neighbors(
        self,
        resource_id: str,
        edge_types: list[str] | None = None,
        depth: int = 1,
        project_id: str | None = None,
    ) -> list[str]:
        """Resource IDs reachable from `resource_id` within `depth` hops.

        Traversal follows edges in both directions (undirected neighborhood).
        Returns unique IDs, excluding the starting resource itself.

        For depth > 1, this does iterative BFS in Python. Keep depth small
        (1 or 2) for interactive use.
        """
        visited: set[str] = {resource_id}
        frontier: set[str] = {resource_id}

        for _ in range(depth):
            if not frontier:
                break
            next_frontier: set[str] = set()
            for node_id in frontier:
                # Outgoing
                for edge in self.edges_from(node_id, edge_types, project_id=project_id):
                    if edge.dst_id not in visited:
                        next_frontier.add(edge.dst_id)
                        visited.add(edge.dst_id)
                # Incoming
                for edge in self.edges_to(node_id, edge_types, project_id=project_id):
                    if edge.src_id not in visited:
                        next_frontier.add(edge.src_id)
                        visited.add(edge.src_id)
            frontier = next_frontier

        visited.discard(resource_id)
        return sorted(visited)

    def between(self, src_id: str, dst_id: str) -> list[ConceptEdge]:
        """All edges between two resources (in either direction)."""
        rows = self.conn.execute(
            """
            SELECT * FROM concept_edges
            WHERE (src_id = ? AND dst_id = ?)
               OR (src_id = ? AND dst_id = ?)
            ORDER BY weight DESC
            """,
            (src_id, dst_id, dst_id, src_id),
        ).fetchall()
        return [self._row_to_edge(r) for r in rows]

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    def _row_to_edge(self, row: sqlite3.Row | dict) -> ConceptEdge:
        d = dict(row)
        metadata_raw = d.get("metadata")
        metadata = json.loads(metadata_raw) if metadata_raw else None
        return ConceptEdge(
            id=d["id"],
            src_id=d["src_id"],
            dst_id=d["dst_id"],
            edge_type=d["edge_type"],
            relation_hint=d.get("relation_hint"),
            weight=d.get("weight", 1.0),
            metadata=metadata,
            created_at=d["created_at"],
            project_id=d.get("project_id"),
        )
