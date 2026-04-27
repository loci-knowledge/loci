"""Project memo — a structured markdown snapshot of a project's current state.

`build_project_memo` is called at the start of reflection and draft pipelines so
every LLM prompt is grounded in the project's recent history, open threads, and
user behaviour signals. It covers:

  - What the project is (profile_md head)
  - Recent edits (node_revisions log)
  - What's been mattering (citation signal over last 30 days)
  - Open threads (pending proposals + dirty/stale nodes)
  - User behaviour signals (pinned nodes, recent reflections)

The result is cached in-process for up to TTL seconds per project_id so rapid
successive calls (draft → reflect in the same server tick) don't hit the DB
twice.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta

# ---------------------------------------------------------------------------
# Simple in-process cache
# ---------------------------------------------------------------------------

_CACHE: dict[str, tuple[float, str]] = {}  # project_id -> (epoch_float, memo_markdown)
_CACHE_TTL = 60  # seconds — short so post-draft reflects see fresh edits


def invalidate_memo_cache(project_id: str) -> None:
    """Remove a project's cached memo so the next call recomputes it."""
    _CACHE.pop(project_id, None)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_project_memo(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    max_recent: int = 15,
    max_open: int = 10,
) -> str:
    """Build and return a structured markdown memo for the project.

    Safe against empty tables (COALESCE / graceful None handling). If
    node_revisions does not yet exist the "Recent edits" section is replaced
    with a fallback message.
    """
    now = time.time()
    cached = _CACHE.get(project_id)
    if cached is not None and now - cached[0] < _CACHE_TTL:
        return cached[1]

    memo = _compute_memo(conn, project_id, max_recent=max_recent, max_open=max_open)
    _CACHE[project_id] = (now, memo)
    return memo


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _compute_memo(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    max_recent: int,
    max_open: int,
) -> str:
    sections: list[str] = []

    # -----------------------------------------------------------------------
    # What this project is
    # -----------------------------------------------------------------------
    row = conn.execute(
        "SELECT profile_md FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    profile_md = (row["profile_md"] if row else None) or ""
    profile_preview = profile_md[:500] if profile_md else "(no profile set)"
    sections.append(f"## What this project is\n\n{profile_preview}")

    # -----------------------------------------------------------------------
    # Recent edits
    # -----------------------------------------------------------------------
    try:
        recent_rows = conn.execute(
            """
            SELECT nr.op, nr.actor, nr.ts, nr.reason, COALESCE(n.title, nr.node_id) AS title
            FROM node_revisions nr
            JOIN project_membership pm ON pm.node_id = nr.node_id AND pm.project_id = ?
            LEFT JOIN nodes n ON n.id = nr.node_id
            WHERE nr.op != 'create'
            ORDER BY nr.ts DESC
            LIMIT ?
            """,
            (project_id, max_recent),
        ).fetchall()
        if recent_rows:
            lines = []
            for r in recent_rows:
                ts_short = (r["ts"] or "")[:10]
                reason = r["reason"] or ""
                lines.append(
                    f"- {r['op']}  \"{r['title']}\"  ({r['actor']}, {ts_short})"
                    + (f" — {reason}" if reason else "")
                )
            sections.append("## Recent edits\n\n" + "\n".join(lines))
        else:
            sections.append("## Recent edits\n\n(none)")
    except sqlite3.OperationalError:
        sections.append(
            "## Recent edits\n\n"
            "(revision log not yet available — run loci reset to upgrade)"
        )

    # -----------------------------------------------------------------------
    # What's been mattering (top nodes by citation signal, last 30 days)
    # -----------------------------------------------------------------------
    cutoff = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    signal_rows = conn.execute(
        """
        SELECT t.node_id, n.title, n.access_count,
               SUM(CASE WHEN t.kind='cited_kept' THEN 1 ELSE 0 END) AS kept,
               SUM(CASE WHEN t.kind='cited_dropped' THEN 1 ELSE 0 END) AS dropped
        FROM traces t
        JOIN nodes n ON n.id = t.node_id
        WHERE t.project_id = ? AND t.ts >= ?
        GROUP BY t.node_id
        ORDER BY
            (SUM(CASE WHEN t.kind='cited_kept' THEN 1 ELSE 0 END) -
             SUM(CASE WHEN t.kind='cited_dropped' THEN 1 ELSE 0 END)) DESC,
            n.access_count DESC
        LIMIT 8
        """,
        (project_id, cutoff),
    ).fetchall()
    if signal_rows:
        lines = [
            f"- \"{r['title']}\" (kept={r['kept']}, dropped={r['dropped']}, "
            f"accesses={r['access_count'] or 0})"
            for r in signal_rows
        ]
        sections.append("## What's been mattering\n\n" + "\n".join(lines))
    else:
        sections.append("## What's been mattering\n\n(no citation signal in last 30 days)")

    # -----------------------------------------------------------------------
    # Open threads
    # -----------------------------------------------------------------------
    open_parts: list[str] = []

    # Pending proposals — payload carries 'about_node_id'; join to nodes for title.
    pending_proposals = conn.execute(
        """
        SELECT p.id, COALESCE(n.title, json_extract(p.payload, '$.about_node_id'), p.id) AS title
        FROM proposals p
        LEFT JOIN nodes n ON n.id = json_extract(p.payload, '$.about_node_id')
        WHERE p.project_id = ? AND p.status = 'pending'
        LIMIT ?
        """,
        (project_id, max_open),
    ).fetchall()
    if pending_proposals:
        proposal_lines = [f"  - [{r['id'][:8]}…] {r['title']}" for r in pending_proposals]
        open_parts.append("**Pending proposals:**\n" + "\n".join(proposal_lines))

    # Dirty/stale nodes in this project.
    stale_rows = conn.execute(
        """
        SELECT n.id, n.title, n.status
        FROM nodes n
        JOIN project_membership pm ON pm.node_id = n.id
        WHERE pm.project_id = ? AND n.status IN ('dirty', 'stale')
        LIMIT ?
        """,
        (project_id, max_open),
    ).fetchall()
    if stale_rows:
        stale_lines = [f"  - [{r['status']}] {r['title']}" for r in stale_rows]
        open_parts.append("**Dirty/stale nodes:**\n" + "\n".join(stale_lines))

    if open_parts:
        sections.append("## Open threads\n\n" + "\n\n".join(open_parts))
    else:
        sections.append("## Open threads\n\n(none)")

    # -----------------------------------------------------------------------
    # User behaviour signals
    # -----------------------------------------------------------------------
    behavior_parts: list[str] = []

    # Recently pinned nodes (added_at column on project_membership).
    pinned_rows = conn.execute(
        """
        SELECT n.title
        FROM nodes n
        JOIN project_membership pm ON pm.node_id = n.id
        WHERE pm.project_id = ? AND pm.role = 'pinned'
        ORDER BY pm.added_at DESC
        LIMIT 5
        """,
        (project_id,),
    ).fetchall()
    if pinned_rows:
        pinned_lines = [f"  - {r['title']}" for r in pinned_rows]
        behavior_parts.append("**Recently pinned:**\n" + "\n".join(pinned_lines))

    # Last 5 reflection summaries.
    reflection_rows = conn.execute(
        """
        SELECT instruction, ts
        FROM agent_reflections
        WHERE project_id = ?
        ORDER BY ts DESC
        LIMIT 5
        """,
        (project_id,),
    ).fetchall()
    if reflection_rows:
        refl_lines = [
            f"  - ({(r['ts'] or '')[:10]}) {r['instruction']}"
            for r in reflection_rows
        ]
        behavior_parts.append("**Recent reflections:**\n" + "\n".join(refl_lines))

    if behavior_parts:
        sections.append("## User behaviour signals\n\n" + "\n\n".join(behavior_parts))
    else:
        sections.append("## User behaviour signals\n\n(none)")

    return "\n\n".join(sections)
