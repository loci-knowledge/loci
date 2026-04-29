"""Lexical retrieval via FTS5 BM25 over raw chunks.

Queries `chunks_fts` (chunk text + section heading) at chunk granularity.
Callers can narrow the search by aspect labels or folder prefix.

Implementation notes:

1. `_sanitise_fts5_query` strips FTS5 reserved characters and turns the
   query into a safe OR-disjunction so partial queries still match.

2. The BM25 weights give text column a strong lead and treat section as a
   light tiebreaker.

3. When `filter_aspects` is given, we join through `resource_aspects` and
   `aspect_vocab` to restrict to resources carrying any of those labels.

4. When `filter_folder` is given, we join through `resource_provenance` and
   filter with a LIKE prefix match.
"""

from __future__ import annotations

import re
import sqlite3

_FTS5_SPECIAL = re.compile(r"[\"():\^*\-]")

_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "in", "on", "at", "to", "of",
    "for", "with", "is", "are", "was", "be", "by", "that", "this",
    "it", "as", "from", "but", "not", "have", "has",
})


def search_lex(
    query: str,
    project_id: str,
    conn: sqlite3.Connection,
    limit: int = 20,
    filter_aspects: list[str] | None = None,
    filter_folder: str | None = None,
) -> list[dict]:
    """BM25 over chunks_fts. Returns list of {chunk_id, resource_id, text, score}.

    `score` is the raw BM25 value from SQLite (smaller = better match). Callers
    that need a higher-is-better score should negate it.

    If `filter_aspects` is given, only chunks from resources that have ANY of
    those aspect labels (via resource_aspects) are returned.

    If `filter_folder` is given, only chunks from resources whose provenance
    folder starts with that string are returned.
    """
    fts_query = _sanitise_fts5_query(query)
    if not fts_query:
        return []

    # We pull more chunk rows than requested so we have enough after
    # the project membership filter.
    chunk_k = max(limit * 4, limit + 20)

    # Build the aspect filter CTE if needed.
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
            f.chunk_id                              AS chunk_id,
            f.raw_id                                AS resource_id,
            bm25(chunks_fts, 1.0, 0.4)              AS score,
            c.text                                  AS text,
            c.section                               AS section
        FROM chunks_fts f
        JOIN raw_chunks c                       ON c.id = f.chunk_id
        JOIN nodes n                            ON n.id = c.raw_id
        JOIN project_effective_members pm       ON pm.node_id = n.id
        {aspect_join}
        {folder_join}
        WHERE chunks_fts MATCH ?
          AND pm.project_id = ?
          AND n.status IN ('live', 'dirty')
        ORDER BY score
        LIMIT ?
    """

    params: tuple = (*aspect_params, *folder_params, fts_query, project_id, chunk_k)
    rows = conn.execute(sql, params).fetchall()

    # Deduplicate to one chunk per resource (the top BM25 hit).
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


# ---------------------------------------------------------------------------
# Internal helpers (also used by concept_expand.py)
# ---------------------------------------------------------------------------


def _sanitise_fts5_query(q: str) -> str:
    """Strip FTS5 special characters and emit a safe OR-disjunction.

    "rotary embeddings cross-attention" → '"rotary" OR "embeddings" OR ...'

    We use OR (not AND) because queries are often incomplete phrases —
    partial-term queries should still surface relevant chunks.
    """
    cleaned = _FTS5_SPECIAL.sub(" ", q)
    tokens = [t for t in cleaned.split() if len(t) >= 2]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


def _query_terms(q: str) -> list[str]:
    """Return lowercase non-stop-word tokens from q (for 'why' strings)."""
    cleaned = _FTS5_SPECIAL.sub(" ", q)
    seen: set[str] = set()
    out: list[str] = []
    for t in cleaned.lower().split():
        if len(t) >= 3 and t not in _STOP_WORDS and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:8]
