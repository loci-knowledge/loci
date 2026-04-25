"""Absorb-time audits.

PLAN.md §Edge cases (5): orphan, broken-support, bloat, thinning. Audits are
read-only — they identify candidates and (where appropriate) flip status, but
they don't delete. The proposal queue is the user's decision surface.

`broken_support` is implemented in `loci.jobs.proposals.detect_broken_supports`
because it produces proposals; we re-export it from here for symmetry.
"""

from __future__ import annotations

import logging
import sqlite3

from loci.graph.models import now_iso
from loci.jobs.proposals import detect_broken_supports

log = logging.getLogger(__name__)


def detect_orphans(conn: sqlite3.Connection, project_id: str) -> int:
    """Mark live interp nodes with 0 edges as `dirty` so they surface for review.

    Orphans aren't intrinsically wrong — sometimes a singleton observation is
    valuable — but enough of them indicates absorb is dropping connections.
    Flipping to `dirty` (instead of `stale`) is a soft nudge: the user sees
    them in the proposal queue at next surface.
    """
    rows = conn.execute(
        """
        SELECT n.id
        FROM nodes n
        JOIN project_membership pm ON pm.node_id = n.id
        WHERE pm.project_id = ? AND pm.role != 'excluded'
          AND n.kind = 'interpretation'
          AND n.status = 'live'
          AND NOT EXISTS (SELECT 1 FROM edges WHERE src = n.id OR dst = n.id)
        """,
        (project_id,),
    ).fetchall()
    if not rows:
        return 0
    ids = [r["id"] for r in rows]
    conn.executemany(
        "UPDATE nodes SET status = 'dirty', updated_at = ? WHERE id = ?",
        [(now_iso(), nid) for nid in ids],
    )
    return len(ids)


def replay_traces(conn: sqlite3.Connection, project_id: str) -> dict:
    """Roll up `traces` into nodes.access_count / last_accessed_at / confidence.

    PLAN.md §Interaction vocabulary:
      - ACCEPT_IMPLICIT: confidence +0.05 on cited live nodes (deferred to absorb)
      - CITED: access_count++, deferred confidence bump

    We compute deltas since the last absorb (or all-time if first absorb), then
    apply them in one pass.
    """
    # For each node, count cited traces since the last 'absorb' job's
    # finished_at (or unbounded if none). Simpler: just count all cited
    # traces since last absorb job ran.
    last_absorb = conn.execute(
        """
        SELECT finished_at FROM jobs
        WHERE kind = 'absorb' AND project_id = ? AND status = 'done'
        ORDER BY finished_at DESC LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    since = last_absorb["finished_at"] if last_absorb else "1970-01-01T00:00:00.000Z"

    # Aggregate cited counts per node.
    cited = conn.execute(
        """
        SELECT node_id, COUNT(*) AS c
        FROM traces
        WHERE project_id = ? AND kind = 'cited' AND ts > ?
        GROUP BY node_id
        """,
        (project_id, since),
    ).fetchall()
    bumped = 0
    for r in cited:
        # Bump access_count by the cited count (already done at retrieve via
        # bump_access — but `cited` traces are post-draft only, so we bump
        # again here for canonicality).
        conn.execute(
            """
            UPDATE nodes SET access_count = access_count + ?,
                              confidence = MAX(0.0, MIN(1.0, confidence + ?)),
                              last_accessed_at = COALESCE(?, last_accessed_at)
            WHERE id = ?
            """,
            (r["c"], 0.05 * r["c"], now_iso(), r["node_id"]),
        )
        bumped += 1
    return {"since": since, "nodes_bumped": bumped}


# Re-export so callers can do `from loci.jobs.audits import detect_broken_supports`.
__all__ = ["detect_orphans", "replay_traces", "detect_broken_supports"]
