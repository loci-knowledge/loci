"""Node repository.

Owns CRUD for `nodes`, `raw_nodes`, `interpretation_nodes`, and `node_tags`.
Also writes to `node_vec` (embeddings) — embeddings live with the node logically
even though the storage is a separate vec0 virtual table.

State machine (status):

    proposed → live          via accept
    live     → dirty         via edit (own edit, or one-hop neighbor edit)
    dirty    → live          via re-derivation at retrieve or absorb time
    live     → stale         via support disappearance (audit)
    *        → dismissed     via explicit dismiss (terminal)

The transitions are advisory at the SQL level (status is a CHECK enum, not a
trigger-enforced FSM). The methods here apply the correct transition for
the action they represent.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

import numpy as np

from loci.embed.local import vec_to_blob
from loci.graph.models import (
    InterpretationNode,
    Node,
    NodeStatus,
    RawNode,
    now_iso,
)


class NodeRepository:
    """All node reads and writes go through this class.

    Constructed with an open SQLite connection. The repo doesn't own the
    connection lifetime — it's a thin object you can instantiate per request.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -----------------------------------------------------------------------
    # Reads
    # -----------------------------------------------------------------------

    def get(self, node_id: str) -> Node | None:
        """Fetch a node by id. Returns the most-specific Pydantic subtype."""
        row = self.conn.execute(
            "SELECT * FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_node(row)

    def get_many(self, node_ids: Iterable[str]) -> list[Node]:
        ids = list(node_ids)
        if not ids:
            return []
        # Build a parameter list for the IN clause. SQLite has no array type;
        # joining `?` repeatedly is the idiomatic path.
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT * FROM nodes WHERE id IN ({placeholders})", tuple(ids)
        ).fetchall()
        # Preserve input order — useful when the caller passes a ranked list.
        by_id = {row["id"]: row for row in rows}
        return [self._row_to_node(by_id[i]) for i in ids if i in by_id]

    def find_raw_by_hash(self, content_hash: str) -> RawNode | None:
        row = self.conn.execute(
            """
            SELECT n.*, r.content_hash, r.canonical_path, r.mime, r.size_bytes,
                   r.source_of_truth
            FROM nodes n
            JOIN raw_nodes r ON r.node_id = n.id
            WHERE r.content_hash = ?
            """,
            (content_hash,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_raw(row)

    # -----------------------------------------------------------------------
    # Writes
    # -----------------------------------------------------------------------

    def create_raw(self, node: RawNode, embedding: np.ndarray | None = None) -> RawNode:
        """Insert a RawNode + raw_nodes row + tags + (optionally) embedding.

        Caller is responsible for the embedding because the embedder is heavy
        and the ingest pipeline batches embeddings. If `embedding` is None we
        skip the vec write — the node is searchable lex but not vec until a
        later `set_embedding()`.
        """
        with self._txn():
            self.conn.execute(
                """
                INSERT INTO nodes(id, kind, subkind, title, body, created_at,
                                  updated_at, last_accessed_at, access_count,
                                  confidence, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    node.id, node.kind, node.subkind, node.title, node.body,
                    node.created_at, node.updated_at, node.last_accessed_at,
                    node.access_count, node.confidence, node.status,
                ),
            )
            self.conn.execute(
                """
                INSERT INTO raw_nodes(node_id, content_hash, canonical_path,
                                       mime, size_bytes, source_of_truth)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    node.id, node.content_hash, node.canonical_path, node.mime,
                    node.size_bytes, int(node.source_of_truth),
                ),
            )
            self._write_tags(node.id, node.tags)
            if embedding is not None:
                self._write_embedding(node.id, embedding)
        return node

    def create_interpretation(
        self,
        node: InterpretationNode,
        embedding: np.ndarray | None = None,
    ) -> InterpretationNode:
        with self._txn():
            self.conn.execute(
                """
                INSERT INTO nodes(id, kind, subkind, title, body, created_at,
                                  updated_at, last_accessed_at, access_count,
                                  confidence, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    node.id, node.kind, node.subkind, node.title, node.body,
                    node.created_at, node.updated_at, node.last_accessed_at,
                    node.access_count, node.confidence, node.status,
                ),
            )
            self.conn.execute(
                """
                INSERT INTO interpretation_nodes(node_id, origin,
                                                  origin_session_id,
                                                  origin_response_id,
                                                  relation_md, overlap_md,
                                                  source_anchor_md,
                                                  angle, rationale_md)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (node.id, node.origin, node.origin_session_id, node.origin_response_id,
                 node.relation_md, node.overlap_md, node.source_anchor_md,
                 node.angle, node.rationale_md),
            )
            self._write_tags(node.id, node.tags)
            if embedding is not None:
                self._write_embedding(node.id, embedding)
        return node

    def set_angle(
        self,
        node_id: str,
        angle: str | None,
        rationale_md: str | None = None,
    ) -> None:
        """Update the angle (and optionally rationale_md) on a relevance interp."""
        sets = ["angle = ?"]
        params: list[object] = [angle]
        if rationale_md is not None:
            sets.append("rationale_md = ?")
            params.append(rationale_md)
        params.append(node_id)
        self.conn.execute(
            f"UPDATE interpretation_nodes SET {', '.join(sets)} WHERE node_id = ?",
            tuple(params),
        )
        self.conn.execute(
            "UPDATE nodes SET updated_at = ? WHERE id = ?", (now_iso(), node_id)
        )

    def update_body(self, node_id: str, *, title: str | None = None,
                    body: str | None = None,
                    tags: list[str] | None = None,
                    new_embedding: np.ndarray | None = None,
                    bump_dirty: bool = True) -> None:
        """Edit a node's body / title / tags. Bumps `updated_at`.

        If `bump_dirty=True` (the default), one-hop neighbours on cites/semantic
        edges are marked `dirty` per PLAN.md §Edge cases (3).
        That neighbour walk is a single UPDATE...WHERE with a subquery — cheap.
        """
        if title is None and body is None and tags is None and new_embedding is None:
            return  # nothing to do
        with self._txn():
            sets: list[str] = ["updated_at = ?"]
            params: list[object] = [now_iso()]
            if title is not None:
                sets.append("title = ?")
                params.append(title)
            if body is not None:
                sets.append("body = ?")
                params.append(body)
            params.append(node_id)
            self.conn.execute(
                f"UPDATE nodes SET {', '.join(sets)} WHERE id = ?",
                tuple(params),
            )
            if tags is not None:
                self.conn.execute("DELETE FROM node_tags WHERE node_id = ?", (node_id,))
                self._write_tags(node_id, tags)
            if new_embedding is not None:
                self._write_embedding(node_id, new_embedding, replace=True)
            if bump_dirty:
                self._mark_neighbors_dirty(node_id)

    def set_status(self, node_id: str, status: NodeStatus) -> None:
        self.conn.execute(
            "UPDATE nodes SET status = ?, updated_at = ? WHERE id = ?",
            (status, now_iso(), node_id),
        )

    def bump_access(self, node_id: str) -> None:
        """Increment access_count and bump last_accessed_at. Called by retrieve."""
        self.conn.execute(
            """
            UPDATE nodes
            SET access_count = access_count + 1,
                last_accessed_at = ?
            WHERE id = ?
            """,
            (now_iso(), node_id),
        )

    def bump_confidence(self, node_id: str, delta: float) -> None:
        """Add `delta` (positive or negative) to confidence, clamped to [0, 1]."""
        # CLAMP via min/max to keep within the schema CHECK.
        self.conn.execute(
            """
            UPDATE nodes
            SET confidence = MAX(0.0, MIN(1.0, confidence + ?))
            WHERE id = ?
            """,
            (delta, node_id),
        )

    def set_embedding(self, node_id: str, vec: np.ndarray) -> None:
        """Write or replace a node's embedding."""
        with self._txn():
            self._write_embedding(node_id, vec, replace=True)

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    def _txn(self):
        # Local convenience so we don't import the connection helper everywhere.
        from loci.db.connection import transaction
        return transaction(self.conn)

    def _write_tags(self, node_id: str, tags: list[str]) -> None:
        if not tags:
            return
        self.conn.executemany(
            "INSERT OR IGNORE INTO node_tags(node_id, tag) VALUES (?, ?)",
            [(node_id, tag) for tag in tags],
        )

    def _write_embedding(self, node_id: str, vec: np.ndarray, *, replace: bool = False) -> None:
        blob = vec_to_blob(vec)
        if replace:
            self.conn.execute("DELETE FROM node_vec WHERE node_id = ?", (node_id,))
        self.conn.execute(
            "INSERT INTO node_vec(node_id, embedding) VALUES (?, ?)",
            (node_id, blob),
        )

    def _mark_neighbors_dirty(self, node_id: str) -> None:
        # Dirty propagates one hop along cites (interp→raw) and derives_from
        # (interp→interp). When a locus changes, the loci that derive from it
        # and the raws it points at may need re-derivation.
        self.conn.execute(
            """
            UPDATE nodes
            SET status = 'dirty', updated_at = ?
            WHERE status = 'live' AND id IN (
                SELECT dst FROM edges
                WHERE src = ? AND type IN ('cites','derives_from')
                UNION
                SELECT src FROM edges
                WHERE dst = ? AND type IN ('cites','derives_from')
            )
            """,
            (now_iso(), node_id, node_id),
        )

    def _row_to_node(self, row: sqlite3.Row) -> Node:
        if row["kind"] == "raw":
            extra = self.conn.execute(
                "SELECT * FROM raw_nodes WHERE node_id = ?", (row["id"],)
            ).fetchone()
            return self._row_to_raw({**dict(row), **dict(extra)} if extra else dict(row))
        else:
            extra = self.conn.execute(
                "SELECT * FROM interpretation_nodes WHERE node_id = ?", (row["id"],)
            ).fetchone()
            return self._row_to_interp({**dict(row), **dict(extra)} if extra else dict(row))

    def _row_to_raw(self, row: dict) -> RawNode:
        return RawNode(
            id=row["id"], kind="raw", subkind=row["subkind"], title=row["title"],
            body=row["body"], created_at=row["created_at"],
            updated_at=row["updated_at"], last_accessed_at=row["last_accessed_at"],
            access_count=row["access_count"], confidence=row["confidence"],
            status=row["status"], tags=self._tags_for(row["id"]),
            content_hash=row["content_hash"], canonical_path=row["canonical_path"],
            mime=row["mime"], size_bytes=row["size_bytes"],
            source_of_truth=bool(row["source_of_truth"]),
        )

    def _row_to_interp(self, row: dict) -> InterpretationNode:
        return InterpretationNode(
            id=row["id"], kind="interpretation", subkind=row["subkind"],
            title=row["title"], body=row["body"], created_at=row["created_at"],
            updated_at=row["updated_at"], last_accessed_at=row["last_accessed_at"],
            access_count=row["access_count"], confidence=row["confidence"],
            status=row["status"], tags=self._tags_for(row["id"]),
            origin=row["origin"], origin_session_id=row.get("origin_session_id"),
            origin_response_id=row.get("origin_response_id"),
            relation_md=row.get("relation_md") or "",
            overlap_md=row.get("overlap_md") or "",
            source_anchor_md=row.get("source_anchor_md") or "",
            angle=row.get("angle"),
            rationale_md=row.get("rationale_md") or "",
        )

    def _tags_for(self, node_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT tag FROM node_tags WHERE node_id = ? ORDER BY tag", (node_id,)
        ).fetchall()
        return [r["tag"] for r in rows]
