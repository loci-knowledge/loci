"""Lexical retrieval via FTS5 BM25.

We query the `nodes_fts` virtual table created in 0001_initial.sql. FTS5 ranks
by BM25 score (lower is better in SQLite's `bm25()` function — it returns the
*negative* score by convention, so smaller = more relevant).

Three things worth knowing if you maintain this:

1. We pass user queries through `_sanitise_fts5_query` because raw user input
   can contain FTS5 reserved characters (`(`, `"`, `*`, `-`, etc.) that crash
   the parser. We don't try to expose FTS5's full query language to the
   caller — that's what a power user would write directly, anyway.

2. The `bm25()` second argument is per-column weights. Title is weighted ~3×
   body to favour title matches without overwhelming long-body relevance.

3. The query joins `nodes_fts` to `nodes` and `project_membership` so we can
   filter to live/dirty nodes inside the right project in one statement.
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


def search(
    conn: sqlite3.Connection,
    project_id: str,
    query: str,
    *,
    k: int = 20,
    include_status: tuple[str, ...] = ("live", "dirty"),
) -> list[LexHit]:
    """Run a BM25 search filtered to project + status. Returns ranked LexHits."""
    fts_query = _sanitise_fts5_query(query)
    if not fts_query:
        return []
    status_placeholders = ",".join("?" * len(include_status))
    sql = f"""
        SELECT
            f.node_id        AS node_id,
            bm25(nodes_fts, 3.0, 1.0, 1.5) AS bm25,
            snippet(nodes_fts, 2, '⟪', '⟫', '…', 12) AS snippet
        FROM nodes_fts f
        JOIN nodes n ON n.id = f.node_id
        JOIN project_membership pm ON pm.node_id = n.id
        WHERE nodes_fts MATCH ?
          AND pm.project_id = ?
          AND pm.role != 'excluded'
          AND n.status IN ({status_placeholders})
        ORDER BY bm25
        LIMIT ?
    """
    rows = conn.execute(sql, (fts_query, project_id, *include_status, k)).fetchall()
    matched_terms = _terms(query)
    return [
        LexHit(
            node_id=row["node_id"],
            bm25=row["bm25"],
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
    # Replace specials with spaces, then collapse whitespace.
    cleaned = _FTS5_SPECIAL.sub(" ", q)
    tokens = [t for t in cleaned.split() if len(t) >= 2]
    if not tokens:
        return ""
    # Quote each token to avoid FTS5 parsing corner cases (e.g. tokens that
    # are FTS5 keywords like "AND", "OR", "NEAR").
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
