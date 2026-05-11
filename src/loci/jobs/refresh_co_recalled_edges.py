"""refresh_co_recalled_edges job — materialise co_recalled concept_edges from usage logs.

For each pair of resources recalled in the same session (within 30 days),
creates or updates a concept_edge with edge_type='co_recalled'.

Weight = co_count / sqrt(count_a * count_b)  (Jaccard cosine normalisation).
Minimum 2 co-occurrences required. Capped at 500 edges per project per run.

Payload shape:
    {"project_id": "<ULID>", "lookback_days": 30}
"""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)

_MAX_EDGES = 500
_MIN_CO_COUNT = 2


async def handle_refresh_co_recalled_edges(
    job: dict, conn: sqlite3.Connection, settings
) -> dict:
    """Materialise co_recalled concept_edges from resource_usage_log."""
    payload = job.get("payload", {})
    project_id = payload.get("project_id") or job.get("project_id")
    lookback_days = int(payload.get("lookback_days", 30))

    if not project_id:
        log.warning("refresh_co_recalled_edges: missing project_id")
        return {"skipped": "missing_project_id"}

    # Compute co-occurrence pairs via SQL.
    rows = conn.execute(
        f"""
        WITH session_resources AS (
            SELECT session_hash, resource_id
            FROM resource_usage_log
            WHERE project_id = ?
              AND session_hash IS NOT NULL
              AND used_at >= datetime('now', '-{lookback_days} days')
            GROUP BY session_hash, resource_id
        ),
        co_occurrences AS (
            SELECT a.resource_id AS src, b.resource_id AS dst,
                   COUNT(*) AS co_count
            FROM session_resources a
            JOIN session_resources b
              ON a.session_hash = b.session_hash
             AND a.resource_id < b.resource_id
            GROUP BY a.resource_id, b.resource_id
            HAVING COUNT(*) >= {_MIN_CO_COUNT}
        ),
        resource_session_counts AS (
            SELECT resource_id, COUNT(DISTINCT session_hash) AS session_count
            FROM session_resources
            GROUP BY resource_id
        )
        SELECT co.src, co.dst,
               co.co_count * 1.0 / SQRT(ra.session_count * rb.session_count) AS weight
        FROM co_occurrences co
        JOIN resource_session_counts ra ON ra.resource_id = co.src
        JOIN resource_session_counts rb ON rb.resource_id = co.dst
        ORDER BY weight DESC
        LIMIT {_MAX_EDGES}
        """,
        (project_id,),
    ).fetchall()

    if not rows:
        log.info(
            "refresh_co_recalled_edges: no co-recalled pairs for project=%s", project_id
        )
        return {"project_id": project_id, "edges_written": 0}

    from loci.graph.models import new_id, now_iso

    ts = now_iso()

    # Delete stale co_recalled edges for this project before reinserting.
    conn.execute(
        "DELETE FROM concept_edges WHERE edge_type = 'co_recalled' AND project_id IS ?",
        (project_id,),
    )

    # Upsert new edges.
    edges_written = 0
    for row in rows:
        src_id, dst_id, weight = row["src"], row["dst"], float(row["weight"])
        # Verify both resources still exist.
        if not conn.execute("SELECT 1 FROM raw_nodes WHERE id = ?", (src_id,)).fetchone():
            continue
        if not conn.execute("SELECT 1 FROM raw_nodes WHERE id = ?", (dst_id,)).fetchone():
            continue
        try:
            conn.execute(
                """
                INSERT INTO concept_edges(
                    id, src_id, dst_id, edge_type,
                    relation_hint, weight, metadata, project_id, created_at
                )
                VALUES (?, ?, ?, 'co_recalled', NULL, ?, NULL, ?, ?)
                """,
                (new_id(), src_id, dst_id, weight, project_id, ts),
            )
            edges_written += 1
        except sqlite3.IntegrityError:
            # Unique constraint hit — update weight instead.
            conn.execute(
                """
                UPDATE concept_edges SET weight = ?
                WHERE src_id = ? AND dst_id = ? AND edge_type = 'co_recalled'
                  AND project_id IS ?
                """,
                (weight, src_id, dst_id, project_id),
            )
            edges_written += 1

    log.info(
        "refresh_co_recalled_edges: project=%s edges=%d lookback=%dd",
        project_id,
        edges_written,
        lookback_days,
    )
    return {"project_id": project_id, "edges_written": edges_written}
