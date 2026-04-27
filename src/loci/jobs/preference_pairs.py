"""Background job: derive preference pairs from a completed draft response.

Each draft that surfaces cited_kept and cited_dropped traces is a natural
source of (positive, negative) pairs for training a reranker. This job
mines those pairs and inserts them into preference_pairs.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from loci.graph.models import new_id

log = logging.getLogger(__name__)


def collect_preference_pairs(
    conn: sqlite3.Connection,
    project_id: str,
    response_id: str,
) -> int:
    """Derive (positive, negative) preference pairs from a completed draft.

    Mines cited_kept vs cited_dropped / cited_replaced traces for the given
    response and inserts them into the preference_pairs table.

    Parameters
    ----------
    conn:
        Open SQLite connection with row_factory set (or plain tuple rows —
        we use positional indexing throughout).
    project_id:
        The project the response belongs to.
    response_id:
        The response to mine. If it has no kept/dropped traces, returns 0.

    Returns
    -------
    int
        The number of preference_pairs rows inserted.
    """
    # 1. Fetch the original query from the response's request JSON.
    row = conn.execute(
        "SELECT request FROM responses WHERE id = ?",
        (response_id,),
    ).fetchone()
    if row is None:
        return 0
    try:
        request_data = json.loads(row[0])
        query = request_data.get("instruction", "")
    except (json.JSONDecodeError, TypeError):
        query = ""

    # 2. Fetch kept, dropped, and replaced node ids for this response.
    trace_rows = conn.execute(
        """
        SELECT node_id, kind
        FROM traces
        WHERE response_id = ?
          AND kind IN ('cited_kept', 'cited_dropped', 'cited_replaced')
        """,
        (response_id,),
    ).fetchall()

    kept: list[str] = []
    dropped: list[str] = []
    replaced: list[str] = []
    for tr in trace_rows:
        nid = tr[0]
        kind = tr[1]
        if kind == "cited_kept":
            kept.append(nid)
        elif kind == "cited_dropped":
            dropped.append(nid)
        elif kind == "cited_replaced":
            replaced.append(nid)

    # 3. No pairs possible if nothing was kept or nothing was dropped/replaced.
    if not kept or (not dropped and not replaced):
        return 0

    inserted = 0
    for positive in kept:
        for negative in dropped:
            pair_id = new_id()
            conn.execute(
                """
                INSERT OR IGNORE INTO preference_pairs
                    (id, project_id, response_id, query,
                     positive_node, negative_node, signal)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pair_id, project_id, response_id, query,
                    positive, negative, "cited_kept_vs_dropped",
                ),
            )
            inserted += 1

        for negative in replaced:
            pair_id = new_id()
            conn.execute(
                """
                INSERT OR IGNORE INTO preference_pairs
                    (id, project_id, response_id, query,
                     positive_node, negative_node, signal)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pair_id, project_id, response_id, query,
                    positive, negative, "cited_kept_vs_replaced",
                ),
            )
            inserted += 1

    return inserted


def enqueue_preference_pairs(
    conn: sqlite3.Connection,
    project_id: str,
    response_id: str,
) -> None:
    """Run collect_preference_pairs, swallowing any exception with a warning.

    Designed to be called inline from the draft pipeline so that a failure
    here never interrupts the caller.
    """
    try:
        n = collect_preference_pairs(conn, project_id, response_id)
        if n:
            log.debug(
                "preference_pairs: inserted %d pairs for response %s",
                n, response_id,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "preference_pairs: failed to collect pairs for response %s: %s",
            response_id, exc,
        )
