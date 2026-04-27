"""User-affinity scores for retrieval personalization.

Computes a per-node affinity score from:
  - recency-decayed access_count (exp half-life 14 days)
  - net cite signal (cited_kept - cited_dropped) from traces
  - pinned membership boost (+1.0)
  - cross-draft repetition: count of distinct responses where node was cited_kept

Result is min-max normalized to [0, 1] across the candidate set so it
composes cleanly with RRF contributions.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import UTC, datetime


def compute_affinity(
    conn: sqlite3.Connection,
    project_id: str,
    candidate_ids: list[str],
    *,
    now_ts: str | None = None,  # override for testing; defaults to current UTC ISO
) -> dict[str, float]:
    """Compute normalized affinity scores for a set of candidate node ids.

    Parameters
    ----------
    conn:
        Open SQLite connection (row_factory should return dict-like rows, but
        we use positional indexing to stay safe).
    project_id:
        The active project — used to scope trace and membership lookups.
    candidate_ids:
        The nodes to score. Any id not found in the DB gets score 0.
    now_ts:
        ISO 8601 UTC string to use as "now" — defaults to current UTC time.
        Intended for deterministic testing.

    Returns
    -------
    dict mapping node_id -> normalized score in [0, 1].
    """
    if not candidate_ids:
        return {}

    now: datetime
    if now_ts is not None:
        now = datetime.fromisoformat(now_ts.replace("Z", "+00:00"))
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
    else:
        now = datetime.now(tz=UTC)

    placeholders = ",".join("?" * len(candidate_ids))
    rows = conn.execute(
        f"""
        SELECT
            n.id,
            n.access_count,
            n.last_accessed_at,
            COALESCE(SUM(CASE WHEN t.kind='cited_kept' THEN 1 ELSE 0 END), 0)
                AS kept,
            COALESCE(SUM(CASE WHEN t.kind='cited_dropped' THEN 1 ELSE 0 END), 0)
                AS dropped,
            COALESCE(COUNT(DISTINCT CASE WHEN t.kind='cited_kept' THEN t.response_id END), 0)
                AS kept_responses,
            COALESCE(MAX(CASE WHEN pm.role='pinned' THEN 1.0 ELSE 0.0 END), 0.0)
                AS is_pinned
        FROM nodes n
        LEFT JOIN traces t
            ON t.node_id = n.id AND t.project_id = ?
        LEFT JOIN project_membership pm
            ON pm.node_id = n.id AND pm.project_id = ?
        WHERE n.id IN ({placeholders})
        GROUP BY n.id
        """,
        (project_id, project_id, *candidate_ids),
    ).fetchall()

    raw_scores: dict[str, float] = {}
    for row in rows:
        node_id = row[0]
        access_count = row[1] or 0
        last_accessed_at = row[2]
        kept = row[3] or 0
        dropped = row[4] or 0
        kept_responses = row[5] or 0
        is_pinned = row[6] or 0.0

        if last_accessed_at is not None:
            try:
                last_dt = datetime.fromisoformat(
                    last_accessed_at.replace("Z", "+00:00")
                )
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=UTC)
                days = (now - last_dt).total_seconds() / 86400.0
            except (ValueError, AttributeError):
                days = 365.0
        else:
            days = 365.0

        decay = math.exp(-0.693 * days / 14.0)

        raw = (
            access_count * decay * 0.3          # recency-decayed access
            + max(0, kept - dropped) * 0.4      # net cite signal
            + kept_responses * 0.2              # cross-draft repetition
            + is_pinned * 1.0                   # pinned boost
        )
        raw_scores[node_id] = raw

    # Ensure every requested id appears in the output (even those not in the DB).
    for nid in candidate_ids:
        raw_scores.setdefault(nid, 0.0)

    # Min-max normalize to [0, 1].
    values = list(raw_scores.values())
    min_v = min(values)
    max_v = max(values)
    spread = max_v - min_v

    if spread == 0.0:
        return {nid: 0.0 for nid in raw_scores}

    return {nid: (v - min_v) / spread for nid, v in raw_scores.items()}
