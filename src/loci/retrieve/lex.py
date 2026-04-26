"""Lexical retrieval via FTS5 BM25.

Two FTS targets:

- `nodes_fts`  — title + body + tags. Used for interpretation nodes (where
                 the locus's slot text + title is what matters).
- `chunks_fts` — chunk text + section heading. Used for `kind="raw"` so
                 BM25 ranks at chunk granularity. The hit returns the
                 winning chunk id + a snippet drawn from it, not from the
                 whole-file body.

Three things worth knowing if you maintain this:

1. We pass user queries through `_sanitise_fts5_query` because raw user input
   can contain FTS5 reserved characters (`(`, `"`, `*`, `-`, etc.) that crash
   the parser. We don't try to expose FTS5's full query language to the
   caller — that's what a power user would write directly, anyway.

2. The `bm25()` second argument is per-column weights. For `nodes_fts`,
   title is weighted ~3× body to favour title matches without overwhelming
   long-body relevance. For `chunks_fts`, text dominates and section is a
   light tiebreaker.

3. Each search joins to `project_effective_members` so we filter to the
   right project (and status set) in a single statement.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

# FTS5 special characters that we strip / quote rather than try to interpret.
# `'` is doubled to escape inside a single-quoted string literal.
_FTS5_SPECIAL = re.compile(r"[\"():\^*\-]")


@dataclass
class LexHit:
    node_id: str
    bm25: float           # raw BM25 (smaller = better)
    snippet: str          # FTS5 snippet for display
    matched_terms: list[str]  # parsed from query, used for `why` strings
    # When the hit comes from chunks_fts (kind="raw"), these point at the
    # winning span. None for interp hits or for raw fallbacks via nodes_fts.
    chunk_id: str | None = None
    chunk_text: str | None = None
    chunk_section: str | None = None


def search(
    conn: sqlite3.Connection,
    project_id: str,
    query: str,
    *,
    k: int = 20,
    include_status: tuple[str, ...] = ("live", "dirty"),
    kind: str | None = None,
) -> list[LexHit]:
    """Run a BM25 search filtered to project + status (+ optional kind).

    `kind="raw"` queries `chunks_fts` and aggregates to one row per raw
    (winning chunk = lowest bm25). Anything else queries `nodes_fts`.
    """
    fts_query = _sanitise_fts5_query(query)
    if not fts_query:
        return []
    matched_terms = _terms(query)
    if kind == "raw":
        return _search_raw_chunks(
            conn, project_id, fts_query, matched_terms,
            k=k, include_status=include_status,
        )
    return _search_nodes_fts(
        conn, project_id, fts_query, matched_terms,
        k=k, include_status=include_status, kind=kind,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _search_raw_chunks(
    conn: sqlite3.Connection,
    project_id: str,
    fts_query: str,
    matched_terms: list[str],
    *,
    k: int,
    include_status: tuple[str, ...],
) -> list[LexHit]:
    status_placeholders = ",".join("?" * len(include_status))
    chunk_k = max(k * 4, k + 20)
    # bm25 weights: text >> section.
    sql = f"""
        SELECT
            f.chunk_id                              AS chunk_id,
            f.raw_id                                AS raw_id,
            bm25(chunks_fts, 1.0, 0.4)              AS bm25,
            snippet(chunks_fts, 2, '⟪', '⟫', '…', 12) AS snippet,
            c.text                                  AS chunk_text,
            c.section                               AS chunk_section
        FROM chunks_fts f
        JOIN raw_chunks c                       ON c.id = f.chunk_id
        JOIN nodes n                            ON n.id = c.raw_id
        JOIN project_effective_members pm       ON pm.node_id = n.id
        WHERE chunks_fts MATCH ?
          AND pm.project_id = ?
          AND n.status IN ({status_placeholders})
        ORDER BY bm25
        LIMIT ?
    """
    params: tuple = (fts_query, project_id, *include_status, chunk_k)
    rows = conn.execute(sql, params).fetchall()

    out: list[LexHit] = []
    seen_raw: set[str] = set()
    for r in rows:
        if r["raw_id"] in seen_raw:
            continue
        seen_raw.add(r["raw_id"])
        out.append(LexHit(
            node_id=r["raw_id"],
            bm25=float(r["bm25"]),
            snippet=r["snippet"] or "",
            matched_terms=matched_terms,
            chunk_id=r["chunk_id"],
            chunk_text=r["chunk_text"],
            chunk_section=r["chunk_section"],
        ))
        if len(out) >= k:
            break

    # Fallback to nodes_fts for raws that aren't chunked yet.
    if len(out) < k:
        fallback_sql = f"""
            SELECT
                f.node_id                                  AS node_id,
                bm25(nodes_fts, 3.0, 1.0, 1.5)             AS bm25,
                snippet(nodes_fts, 2, '⟪', '⟫', '…', 12)   AS snippet
            FROM nodes_fts f
            JOIN nodes n                       ON n.id = f.node_id
            JOIN project_effective_members pm  ON pm.node_id = n.id
            WHERE nodes_fts MATCH ?
              AND pm.project_id = ?
              AND n.status IN ({status_placeholders})
              AND n.kind = 'raw'
              AND NOT EXISTS (SELECT 1 FROM raw_chunks rc WHERE rc.raw_id = n.id)
            ORDER BY bm25
            LIMIT ?
        """
        seen_pred_params: tuple = ()
        if seen_raw:
            seen_pred = " AND n.id NOT IN (" + ",".join("?" * len(seen_raw)) + ")"
            fallback_sql = fallback_sql.replace("ORDER BY bm25", seen_pred + " ORDER BY bm25")
            seen_pred_params = tuple(seen_raw)
        fb_params: tuple = (
            fts_query, project_id, *include_status, *seen_pred_params, k - len(out),
        )
        for r in conn.execute(fallback_sql, fb_params).fetchall():
            out.append(LexHit(
                node_id=r["node_id"],
                bm25=float(r["bm25"]),
                snippet=r["snippet"] or "",
                matched_terms=matched_terms,
            ))
            if len(out) >= k:
                break
    return out


def _search_nodes_fts(
    conn: sqlite3.Connection,
    project_id: str,
    fts_query: str,
    matched_terms: list[str],
    *,
    k: int,
    include_status: tuple[str, ...],
    kind: str | None,
) -> list[LexHit]:
    status_placeholders = ",".join("?" * len(include_status))
    kind_clause = "AND n.kind = ?" if kind else ""
    sql = f"""
        SELECT
            f.node_id        AS node_id,
            bm25(nodes_fts, 3.0, 1.0, 1.5) AS bm25,
            snippet(nodes_fts, 2, '⟪', '⟫', '…', 12) AS snippet
        FROM nodes_fts f
        JOIN nodes n ON n.id = f.node_id
        JOIN project_effective_members pm ON pm.node_id = n.id
        WHERE nodes_fts MATCH ?
          AND pm.project_id = ?
          AND n.status IN ({status_placeholders})
          {kind_clause}
        ORDER BY bm25
        LIMIT ?
    """
    params: tuple = (fts_query, project_id, *include_status)
    if kind:
        params = (*params, kind)
    params = (*params, k)
    rows = conn.execute(sql, params).fetchall()
    return [
        LexHit(
            node_id=row["node_id"],
            bm25=float(row["bm25"]),
            snippet=row["snippet"] or "",
            matched_terms=matched_terms,
        )
        for row in rows
    ]


def _sanitise_fts5_query(q: str) -> str:
    """Strip FTS5 specials and turn the query into a safe disjunction.

    "rotary embeddings cross-attention" → '"rotary" OR "embeddings" OR "cross" OR "attention"'

    We disjunct rather than implicit-AND because users phrase queries
    incompletely — "tell me about rotary attention pattern" should match a
    doc that has "rotary" and "attention" but not "pattern" verbatim.
    """
    cleaned = _FTS5_SPECIAL.sub(" ", q)
    tokens = [t for t in cleaned.split() if len(t) >= 2]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


def _terms(q: str) -> list[str]:
    """Return the lowercase terms we'll cite in `why` strings."""
    cleaned = _FTS5_SPECIAL.sub(" ", q)
    seen: set[str] = set()
    out: list[str] = []
    for t in cleaned.lower().split():
        if len(t) >= 3 and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:6]
