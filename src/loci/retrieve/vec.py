"""Vector retrieval via sqlite-vec.

Two index targets:

- `node_vec`  — one embedding per node. Used for interpretation nodes
                (and as a fallback for raws that haven't been chunked yet).
- `chunk_vec` — one embedding per chunk of a raw node. Used as the primary
                path for `kind="raw"` because span-level grounding is what
                lets the LLM (and the user reading citations) see which
                part of a long source actually carries the claim.

Two flavours of public function:

- `search_text(query, k)` — embed the query text, then ANN-search.
- `search_vec(vec, k)` — caller already has a vector (e.g. from HyDE).

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
    distance: float            # L2 over unit-norm vectors; smaller = better
    # When the hit comes from the chunk-level index, these point at the
    # winning span. None for interp hits or raw-node-level fallbacks.
    chunk_id: str | None = None
    chunk_text: str | None = None
    chunk_section: str | None = None


def search_text(
    conn: sqlite3.Connection,
    project_id: str,
    query: str,
    *,
    k: int = 20,
    include_status: tuple[str, ...] = ("live", "dirty"),
    embedder: Embedder | None = None,
    kind: str | None = None,
) -> list[VecHit]:
    embedder = embedder or get_embedder()
    if not query.strip():
        return []
    vec = embedder.encode(query)
    return search_vec(
        conn, project_id, vec,
        k=k, include_status=include_status, kind=kind,
    )


def search_vec(
    conn: sqlite3.Connection,
    project_id: str,
    vec: np.ndarray,
    *,
    k: int = 20,
    include_status: tuple[str, ...] = ("live", "dirty"),
    kind: str | None = None,
) -> list[VecHit]:
    """ANN search with project + status (+ optional kind) filtering.

    For `kind="raw"` we hit `chunk_vec` and aggregate to one row per raw,
    keeping the best (smallest-distance) chunk as the "winning span". For
    interpretation nodes (or when `kind` is None and we want the legacy
    behaviour) we hit `node_vec`.

    The chunk path falls back to `node_vec` for any raws that don't yet
    have chunks (older databases pre-0002 or raws written before backfill).
    """
    if kind == "raw":
        return _search_raw_chunks(
            conn, project_id, vec, k=k, include_status=include_status,
        )
    return _search_node_vec(
        conn, project_id, vec, k=k, include_status=include_status, kind=kind,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _search_raw_chunks(
    conn: sqlite3.Connection,
    project_id: str,
    vec: np.ndarray,
    *,
    k: int,
    include_status: tuple[str, ...],
) -> list[VecHit]:
    blob = vec_to_blob(vec)
    status_placeholders = ",".join("?" * len(include_status))
    # Pull more chunks than we strictly need so the dedupe-to-raw step still
    # has K winners after collapsing multi-chunk hits from the same raw.
    chunk_k = max(k * 4, k + 20)
    sql = f"""
        SELECT v.chunk_id        AS chunk_id,
               v.distance         AS distance,
               c.raw_id           AS raw_id,
               c.text             AS chunk_text,
               c.section          AS chunk_section
        FROM chunk_vec v
        JOIN raw_chunks c                   ON c.id = v.chunk_id
        JOIN nodes n                        ON n.id = c.raw_id
        JOIN project_effective_members pm   ON pm.node_id = n.id
        WHERE v.embedding MATCH ? AND k = ?
          AND pm.project_id = ?
          AND n.status IN ({status_placeholders})
        ORDER BY v.distance
    """
    params: tuple = (blob, chunk_k, project_id, *include_status)
    rows = conn.execute(sql, params).fetchall()

    out: list[VecHit] = []
    seen_raw: set[str] = set()
    for r in rows:
        if r["raw_id"] in seen_raw:
            continue
        seen_raw.add(r["raw_id"])
        out.append(VecHit(
            node_id=r["raw_id"],
            distance=float(r["distance"]),
            chunk_id=r["chunk_id"],
            chunk_text=r["chunk_text"],
            chunk_section=r["chunk_section"],
        ))
        if len(out) >= k:
            break

    # Fallback: any raw in the project that has a row in `node_vec` but no
    # chunks yet (old databases). Mix those in by distance — capped so we
    # never return more than k overall.
    if len(out) < k:
        seen = ",".join("?" * len(seen_raw)) if seen_raw else "''"
        skip_clause = (
            f"AND n.id NOT IN ({seen})" if seen_raw else ""
        )
        fallback_sql = f"""
            SELECT v.node_id AS node_id, v.distance AS distance
            FROM node_vec v
            JOIN nodes n                       ON n.id = v.node_id
            JOIN project_effective_members pm  ON pm.node_id = n.id
            WHERE v.embedding MATCH ? AND k = ?
              AND pm.project_id = ?
              AND n.status IN ({status_placeholders})
              AND n.kind = 'raw'
              AND NOT EXISTS (SELECT 1 FROM raw_chunks rc WHERE rc.raw_id = n.id)
              {skip_clause}
            ORDER BY v.distance
        """
        fb_params: tuple = (blob, k - len(out), project_id, *include_status)
        if seen_raw:
            fb_params = (*fb_params, *tuple(seen_raw))
        for r in conn.execute(fallback_sql, fb_params).fetchall():
            out.append(VecHit(
                node_id=r["node_id"], distance=float(r["distance"]),
            ))
            if len(out) >= k:
                break
    return out


def _search_node_vec(
    conn: sqlite3.Connection,
    project_id: str,
    vec: np.ndarray,
    *,
    k: int,
    include_status: tuple[str, ...],
    kind: str | None,
) -> list[VecHit]:
    blob = vec_to_blob(vec)
    status_placeholders = ",".join("?" * len(include_status))
    kind_clause = "AND n.kind = ?" if kind else ""
    sql = f"""
        SELECT v.node_id AS node_id, v.distance AS distance
        FROM node_vec v
        JOIN nodes n ON n.id = v.node_id
        JOIN project_effective_members pm ON pm.node_id = n.id
        WHERE v.embedding MATCH ? AND k = ?
          AND pm.project_id = ?
          AND n.status IN ({status_placeholders})
          {kind_clause}
        ORDER BY v.distance
    """
    params: tuple = (blob, k, project_id, *include_status)
    if kind:
        params = (*params, kind)
    rows = conn.execute(sql, params).fetchall()
    return [VecHit(node_id=r["node_id"], distance=float(r["distance"])) for r in rows]
