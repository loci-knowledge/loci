"""Aspect embedding repository.

Stores and retrieves pre-computed unit-normalised float32 embeddings for
aspect vocabulary labels. Used to match query keywords against aspects via
cosine similarity instead of pure string fuzzy matching.

Blob format: little-endian packed float32 (same as vec_to_blob / blob_to_vec
in loci.embed.local). Stored in the `aspect_embeddings` table.
"""

from __future__ import annotations

import logging
import sqlite3

import numpy as np

from loci.embed.local import blob_to_vec  # noqa: F401 — re-exported for callers
from loci.embed.local import vec_to_blob
from loci.graph.models import now_iso

log = logging.getLogger(__name__)


def _unpack(blob: bytes) -> np.ndarray:
    """Unpack a BLOB into a float32 ndarray (dim inferred from length)."""
    return np.frombuffer(blob, dtype=np.float32).copy()


class AspectEmbedRepository:
    """Store and retrieve aspect-label embeddings.

    Constructed with an open SQLite connection. Does not own the connection
    lifetime.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def store(self, aspect_id: str, vec: np.ndarray, model_id: str) -> None:
        """Upsert an embedding row.

        `vec` must be float32 and unit-normalised (||v|| ≈ 1).  Passing a
        non-float32 array is accepted and converted internally.
        """
        if vec.dtype != np.float32:
            vec = vec.astype(np.float32)
        blob = vec_to_blob(vec)
        ts = now_iso()
        self.conn.execute(
            """
            INSERT INTO aspect_embeddings(aspect_id, embedding, model_id, computed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(aspect_id) DO UPDATE SET
                embedding   = excluded.embedding,
                model_id    = excluded.model_id,
                computed_at = excluded.computed_at
            """,
            (aspect_id, blob, model_id, ts),
        )

    def get(self, aspect_id: str) -> np.ndarray | None:
        """Return the stored embedding for *aspect_id*, or None if absent."""
        row = self.conn.execute(
            "SELECT embedding FROM aspect_embeddings WHERE aspect_id = ?",
            (aspect_id,),
        ).fetchone()
        if row is None:
            return None
        return _unpack(row["embedding"])

    def cosine_match(
        self,
        query_vec: np.ndarray,
        project_id: str | None,
        top_k: int = 10,
        threshold: float = 0.5,
    ) -> list[tuple[str, float]]:
        """Return *(aspect_label, cosine_sim)* pairs ordered descending.

        Fetches all stored embeddings from the DB, unpacks the BLOBs, computes
        cosine similarity (dot product — vectors are unit-normalised), and
        returns the top-*k* results above *threshold*.

        When *project_id* is given, only aspects that are global
        (project_id IS NULL) or belong to the specified project are considered.
        This is fine at vocabulary scale (< 10 000 rows).
        """
        if project_id is not None:
            rows = self.conn.execute(
                """
                SELECT ae.aspect_id, ae.embedding, av.label
                FROM aspect_embeddings ae
                JOIN aspect_vocab av ON av.id = ae.aspect_id
                WHERE av.project_id IS NULL OR av.project_id = ?
                """,
                (project_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT ae.aspect_id, ae.embedding, av.label
                FROM aspect_embeddings ae
                JOIN aspect_vocab av ON av.id = ae.aspect_id
                """,
            ).fetchall()

        if not rows:
            return []

        if query_vec.dtype != np.float32:
            query_vec = query_vec.astype(np.float32)

        results: list[tuple[str, float]] = []
        for row in rows:
            stored_vec = _unpack(row["embedding"])
            # Dot product of two unit-normalised vectors == cosine similarity.
            sim = float(np.dot(query_vec, stored_vec))
            if sim >= threshold:
                results.append((row["label"], sim))

        results.sort(key=lambda x: -x[1])
        return results[:top_k]
