"""Source (raw node) repository.

Owns CRUD for `nodes`, `raw_nodes`, and `node_tags`. Also writes to `node_vec`
(whole-file embeddings) and delegates chunk writes to `loci.ingest.chunks`.

This is a refactored version of the old `nodes.py` / `NodeRepository`. The
interpretation-node read/write paths have been removed; only raw-source
operations remain.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import TYPE_CHECKING

import numpy as np

from loci.embed.local import vec_to_blob
from loci.graph.models import RawNode, now_iso

if TYPE_CHECKING:
    pass


class SourceRepository:
    """All raw-node reads and writes go through this class.

    Constructed with an open SQLite connection. The repo does not own the
    connection lifetime — instantiate per request or reuse within a thread.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -----------------------------------------------------------------------
    # Reads
    # -----------------------------------------------------------------------

    def get(self, node_id: str) -> RawNode | None:
        """Fetch a raw node by id. Returns None if not found or not a raw node."""
        row = self.conn.execute(
            "SELECT * FROM nodes WHERE id = ? AND kind = 'raw'", (node_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_raw(row)

    def get_many(self, node_ids: Iterable[str]) -> list[RawNode]:
        """Fetch multiple raw nodes by id. Preserves input order; skips missing."""
        ids = list(node_ids)
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT * FROM nodes WHERE id IN ({placeholders}) AND kind = 'raw'",
            tuple(ids),
        ).fetchall()
        by_id = {row["id"]: row for row in rows}
        return [self._row_to_raw(by_id[i]) for i in ids if i in by_id]

    def get_by_hash(self, content_hash: str) -> RawNode | None:
        """Find a raw node by its content hash. Returns None if not found."""
        row = self.conn.execute(
            """
            SELECT n.*, r.content_hash, r.canonical_path, r.mime, r.size_bytes,
                   r.source_of_truth
            FROM nodes n
            JOIN raw_nodes r ON r.id = n.id
            WHERE r.content_hash = ?
            """,
            (content_hash,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_raw(row)

    def list_by_project(self, project_id: str) -> list[RawNode]:
        """All raw nodes in a project's effective membership."""
        rows = self.conn.execute(
            """
            SELECT n.*, r.content_hash, r.canonical_path, r.mime, r.size_bytes,
                   r.source_of_truth
            FROM nodes n
            JOIN raw_nodes r ON r.id = n.id
            JOIN project_effective_members pm ON pm.node_id = n.id
            WHERE pm.project_id = ? AND n.kind = 'raw'
            ORDER BY n.created_at DESC
            """,
            (project_id,),
        ).fetchall()
        return [self._row_to_raw(row) for row in rows]

    def list_by_workspace(self, workspace_id: str) -> list[RawNode]:
        """All raw nodes that belong to a workspace."""
        rows = self.conn.execute(
            """
            SELECT n.*, r.content_hash, r.canonical_path, r.mime, r.size_bytes,
                   r.source_of_truth
            FROM nodes n
            JOIN raw_nodes r ON r.id = n.id
            JOIN workspace_membership wm ON wm.node_id = n.id
            WHERE wm.workspace_id = ? AND n.kind = 'raw'
            ORDER BY n.created_at DESC
            """,
            (workspace_id,),
        ).fetchall()
        return [self._row_to_raw(row) for row in rows]

    def search_by_title(self, query: str, project_id: str | None = None, limit: int = 20) -> list[RawNode]:
        """Simple case-insensitive title search. Used for quick lookups."""
        if project_id:
            rows = self.conn.execute(
                """
                SELECT n.*, r.content_hash, r.canonical_path, r.mime,
                       r.size_bytes, r.source_of_truth
                FROM nodes n
                JOIN raw_nodes r ON r.id = n.id
                JOIN project_effective_members pm ON pm.node_id = n.id
                WHERE pm.project_id = ? AND n.kind = 'raw'
                  AND n.title LIKE ?
                ORDER BY n.access_count DESC, n.created_at DESC
                LIMIT ?
                """,
                (project_id, f"%{query}%", limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT n.*, r.content_hash, r.canonical_path, r.mime,
                       r.size_bytes, r.source_of_truth
                FROM nodes n
                JOIN raw_nodes r ON r.id = n.id
                WHERE n.kind = 'raw' AND n.title LIKE ?
                ORDER BY n.access_count DESC, n.created_at DESC
                LIMIT ?
                """,
                (f"%{query}%", limit),
            ).fetchall()
        return [self._row_to_raw(row) for row in rows]

    # -----------------------------------------------------------------------
    # Writes
    # -----------------------------------------------------------------------

    def insert(
        self,
        node: RawNode,
        embedding: np.ndarray | None = None,
        *,
        chunks: list | None = None,
        chunk_embeddings: np.ndarray | None = None,
    ) -> RawNode:
        """Insert a RawNode + raw_nodes row + tags + optional embeddings/chunks.

        Two embedding paths:
        - `chunks` + `chunk_embeddings`: preferred path. Span-level vectors
          land in `chunk_vec` via `loci.ingest.chunks.write_chunks`.
        - `embedding`: legacy whole-file vector, written to `node_vec`.

        Both can be supplied simultaneously.
        """
        from loci.ingest.chunks import write_chunks  # local import avoids cycle

        with self._txn():
            self.conn.execute(
                """
                INSERT INTO nodes(id, kind, subkind, title, body, created_at,
                                  updated_at, last_accessed_at, access_count,
                                  status)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    node.id, node.kind, node.subkind, node.title, node.body,
                    node.created_at, node.updated_at, node.last_accessed_at,
                    node.access_count, node.status,
                ),
            )
            self.conn.execute(
                """
                INSERT INTO raw_nodes(id, content_hash, canonical_path,
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
            if chunks:
                write_chunks(self.conn, node.id, chunks, chunk_embeddings)
        return node

    def update(
        self,
        node_id: str,
        *,
        title: str | None = None,
        body: str | None = None,
        tags: list[str] | None = None,
        new_embedding: np.ndarray | None = None,
    ) -> None:
        """Update mutable fields on a raw node. Bumps `updated_at`."""
        if title is None and body is None and tags is None and new_embedding is None:
            return

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

    def delete(self, node_id: str) -> None:
        """Hard-delete a raw node and all associated data."""
        with self._txn():
            self.conn.execute(
                "DELETE FROM project_membership WHERE node_id = ?", (node_id,)
            )
            self.conn.execute(
                "DELETE FROM workspace_membership WHERE node_id = ?", (node_id,)
            )
            self.conn.execute("DELETE FROM node_vec WHERE node_id = ?", (node_id,))
            self.conn.execute("DELETE FROM node_tags WHERE node_id = ?", (node_id,))
            # chunk_vec rows are deleted via ON DELETE CASCADE on raw_chunks
            self.conn.execute(
                "DELETE FROM raw_chunks WHERE raw_id = ?", (node_id,)
            )
            self.conn.execute(
                "DELETE FROM raw_nodes WHERE id = ?", (node_id,)
            )
            self.conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))

    def set_status(self, node_id: str, status: str) -> None:
        """Update the status column of a raw node."""
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
        """Add `delta` to confidence, clamped to [0.0, 1.0]."""
        self.conn.execute(
            """
            UPDATE nodes
            SET confidence = MAX(0.0, MIN(1.0, confidence + ?))
            WHERE id = ?
            """,
            (delta, node_id),
        )

    def set_embedding(self, node_id: str, vec: np.ndarray) -> None:
        """Write or replace the whole-file embedding for a node."""
        with self._txn():
            self._write_embedding(node_id, vec, replace=True)

    def add_tags(self, node_id: str, tags: list[str]) -> None:
        """Add tags to a node. Idempotent; ignores duplicates."""
        self._write_tags(node_id, tags)

    def remove_tags(self, node_id: str, tags: list[str]) -> None:
        """Remove specific tags from a node."""
        if not tags:
            return
        placeholders = ",".join("?" * len(tags))
        self.conn.execute(
            f"DELETE FROM node_tags WHERE node_id = ? AND tag IN ({placeholders})",
            (node_id, *tags),
        )

    def replace_tags(self, node_id: str, tags: list[str]) -> None:
        """Replace the full tag set for a node."""
        with self._txn():
            self.conn.execute("DELETE FROM node_tags WHERE node_id = ?", (node_id,))
            self._write_tags(node_id, tags)

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    def _txn(self):
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

    def _tags_for(self, node_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT tag FROM node_tags WHERE node_id = ? ORDER BY tag", (node_id,)
        ).fetchall()
        return [r["tag"] for r in rows]

    def _row_to_raw(self, row: sqlite3.Row | dict) -> RawNode:
        d = dict(row)
        # raw_nodes columns may be joined in or may require a sub-query
        if "content_hash" not in d:
            extra = self.conn.execute(
                "SELECT * FROM raw_nodes WHERE id = ?", (d["id"],)
            ).fetchone()
            if extra:
                d.update(dict(extra))
        return RawNode(
            id=d["id"],
            kind="raw",
            subkind=d.get("subkind", "txt"),
            title=d.get("title", ""),
            body=d.get("body") or "",
            created_at=d.get("created_at", now_iso()),
            updated_at=d.get("updated_at", now_iso()),
            last_accessed_at=d.get("last_accessed_at"),
            access_count=d.get("access_count", 0),
            confidence=d.get("confidence", 1.0),
            status=d.get("status", "live"),
            tags=self._tags_for(d["id"]),
            content_hash=d.get("content_hash", ""),
            canonical_path=d.get("canonical_path", ""),
            mime=d.get("mime", ""),
            size_bytes=d.get("size_bytes", 0),
            source_of_truth=bool(d.get("source_of_truth", True)),
        )
