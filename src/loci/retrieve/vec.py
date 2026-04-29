"""Vector retrieval via sqlite-vec over raw chunks.

Queries `chunk_vec` (one embedding per chunk) using ANN search. Callers can
narrow the search by aspect labels or folder prefix.

Distance semantics: vec0 returns L2 distance. Because embeddings are
unit-normalised at embed time (see `loci.embed.local.Embedder`),
`distance² = 2 − 2·cos`, so smaller distance ↔ higher cosine similarity.
`score` in the returned dicts is the raw distance; callers that need a
higher-is-better score should negate it or convert via `1 − dist/2`.

Callers that already have a vector (e.g. from HyDE) use `search_vec`;
callers that only have query text use `search_text` which embeds first.
"""

from __future__ import annotations

import sqlite3

import numpy as np

from loci.embed.local import Embedder, get_embedder, vec_to_blob


def search_text(
    query: str,
    project_id: str,
    conn: sqlite3.Connection,
    limit: int = 20,
    filter_aspects: list[str] | None = None,
    filter_folder: str | None = None,
    embedder: Embedder | None = None,
) -> list[dict]:
    """Embed `query` then delegate to `search_vec`."""
    if not query.strip():
        return []
    emb = (embedder or get_embedder()).encode(query)
    return search_vec(
        emb, project_id, conn,
        limit=limit,
        filter_aspects=filter_aspects,
        filter_folder=filter_folder,
    )


def search_vec(
    query_vec: np.ndarray,
    project_id: str,
    conn: sqlite3.Connection,
    limit: int = 20,
    filter_aspects: list[str] | None = None,
    filter_folder: str | None = None,
) -> list[dict]:
    """ANN over chunk_vec. Returns list of {chunk_id, resource_id, text, score}.

    `score` is the L2 distance from sqlite-vec (smaller = better match).

    If `filter_aspects` is given, only chunks from resources that have ANY of
    those aspect labels (via resource_aspects) are returned.

    If `filter_folder` is given, only chunks from resources whose provenance
    folder starts with that string are returned.
    """
    blob = vec_to_blob(query_vec)

    # Pull more chunks than requested so the per-resource deduplication step
    # still has enough candidates after collapsing multi-chunk hits.
    chunk_k = max(limit * 4, limit + 20)

    aspect_join = ""
    aspect_params: tuple = ()
    if filter_aspects:
        placeholders = ",".join("?" * len(filter_aspects))
        aspect_join = f"""
            JOIN resource_aspects ra  ON ra.resource_id = c.raw_id
            JOIN aspect_vocab av      ON av.id = ra.aspect_id
                                      AND av.label IN ({placeholders})
        """
        aspect_params = tuple(filter_aspects)

    folder_join = ""
    folder_params: tuple = ()
    if filter_folder:
        folder_join = """
            JOIN resource_provenance rp ON rp.resource_id = c.raw_id
                                        AND rp.folder LIKE ?
        """
        folder_params = (filter_folder + "%",)

    sql = f"""
        SELECT
            v.chunk_id      AS chunk_id,
            v.distance      AS score,
            c.raw_id        AS resource_id,
            c.text          AS text,
            c.section       AS section
        FROM chunk_vec v
        JOIN raw_chunks c                       ON c.id = v.chunk_id
        JOIN nodes n                            ON n.id = c.raw_id
        JOIN project_effective_members pm       ON pm.node_id = n.id
        {aspect_join}
        {folder_join}
        WHERE v.embedding MATCH ? AND k = ?
          AND pm.project_id = ?
          AND n.status IN ('live', 'dirty')
        ORDER BY v.distance
    """

    params: tuple = (*aspect_params, *folder_params, blob, chunk_k, project_id)
    rows = conn.execute(sql, params).fetchall()

    # Deduplicate to one chunk per resource (smallest distance = best span).
    out: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        resource_id = r["resource_id"]
        if resource_id in seen:
            continue
        seen.add(resource_id)
        out.append({
            "chunk_id": r["chunk_id"],
            "resource_id": resource_id,
            "text": r["text"] or "",
            "score": float(r["score"]),
            "section": r["section"],
        })
        if len(out) >= limit:
            break

    return out
