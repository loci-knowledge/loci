"""Vector retrieval via sqlite-vec.

Two flavours:

- `search_text(query, k)` — embed the query text, then ANN-search.
- `search_vec(vec, k)` — caller already has a vector (e.g. from HyDE).

Both filter to the project and status set in a single SQL statement: the
sqlite-vec `MATCH` operator combines with regular `WHERE` clauses.

Distance semantics: vec0 returns L2 distance. Because we unit-normalise at
embed time (see `loci.embed.local.Embedder.encode_batch`),
`distance² = 2 - 2·cos`, so smaller distance ↔ higher cosine similarity. We
report the distance directly; the orchestrator converts to a [0,1] score
where needed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np

from loci.embed.local import Embedder, get_embedder, vec_to_blob


@dataclass
class VecHit:
    node_id: str
    distance: float       # L2 over unit-norm vectors; smaller = better


def search_text(
    conn: sqlite3.Connection,
    project_id: str,
    query: str,
    *,
    k: int = 20,
    include_status: tuple[str, ...] = ("live", "dirty"),
    embedder: Embedder | None = None,
) -> list[VecHit]:
    embedder = embedder or get_embedder()
    if not query.strip():
        return []
    vec = embedder.encode(query)
    return search_vec(conn, project_id, vec, k=k, include_status=include_status)


def search_vec(
    conn: sqlite3.Connection,
    project_id: str,
    vec: np.ndarray,
    *,
    k: int = 20,
    include_status: tuple[str, ...] = ("live", "dirty"),
) -> list[VecHit]:
    """ANN search with project + status filtering."""
    blob = vec_to_blob(vec)
    status_placeholders = ",".join("?" * len(include_status))
    # sqlite-vec requires `embedding MATCH ?` and `k = ?` together. We then
    # compose with regular WHERE clauses via JOIN; the optimizer pushes the
    # JOINs after the MATCH (which is fine — vec0 returns at most `k` rows).
    sql = f"""
        SELECT v.node_id AS node_id, v.distance AS distance
        FROM node_vec v
        JOIN nodes n ON n.id = v.node_id
        JOIN project_effective_members pm ON pm.node_id = n.id
        WHERE v.embedding MATCH ? AND k = ?
          AND pm.project_id = ?
          AND n.status IN ({status_placeholders})
        ORDER BY v.distance
    """
    rows = conn.execute(sql, (blob, k, project_id, *include_status)).fetchall()
    return [VecHit(node_id=r["node_id"], distance=r["distance"]) for r in rows]
