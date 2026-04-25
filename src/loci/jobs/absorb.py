"""Absorb job — the checkpoint pipeline.

PLAN.md §Background jobs: "Absorb runs the contradiction pass, rebuilds the
proposal queue, replays traces into access_count / last_accessed_at /
confidence, runs the orphan/broken-support/bloat audits, and re-runs
community detection. It's expensive (multiple LLM calls); clients enqueue and
don't block."

We run the steps in dependency order:

    1. validate filesystem (raw source_of_truth audit)
    2. replay traces → access_count, confidence, last_accessed_at
    3. detect_orphans (flip orphans to dirty)
    4. detect_broken_supports (mark stale, file proposals)
    5. detect_aliases (cosine > 0.92 → propose `aliases`)
    6. detect_forgetting_candidates (low conf + no access → propose dismiss)
    7. contradiction_pass (LLM-mediated; gated on Anthropic key)
    8. communities (igraph/leidenalg; gated on optional dep)

Each step returns a summary dict; we collate them all into the job result.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from loci.ingest.dependencies import extract_and_write as _extract_deps
from loci.jobs import audits, communities, contradiction, proposals

log = logging.getLogger(__name__)


def run(conn: sqlite3.Connection, project_id: str | None, payload: dict) -> dict:
    if project_id is None:
        raise ValueError("absorb requires a project_id")
    log.info("absorb: starting for project=%s", project_id)
    summary: dict[str, object] = {"project_id": project_id, "steps": {}}

    # 1. Filesystem audit on raw nodes — flag missing files.
    summary["steps"]["fs_audit"] = _fs_audit(conn, project_id)

    # 2. Trace replay
    summary["steps"]["replay_traces"] = audits.replay_traces(conn, project_id)

    # 3. Orphan audit
    summary["steps"]["orphans"] = {"flipped_to_dirty": audits.detect_orphans(conn, project_id)}

    # 4. Broken-support tensions
    summary["steps"]["broken_supports"] = {
        "proposals": len(audits.detect_broken_supports(conn, project_id)),
    }

    # 5. Alias detection
    summary["steps"]["aliases"] = {
        "proposals": len(proposals.detect_aliases(conn, project_id)),
    }

    # 6. Forgetting
    summary["steps"]["forgetting"] = {
        "proposals": len(proposals.detect_forgetting_candidates(conn, project_id)),
    }

    # 7. Contradiction pass (LLM-gated)
    summary["steps"]["contradiction"] = contradiction.run_pass(conn, project_id)

    # 8. Communities (igraph-gated)
    summary["steps"]["communities"] = communities.run(conn, project_id)

    # 9. Co-citation edges — interp pairs that share a cited raw get semantic
    summary["steps"]["co_citation"] = _update_co_citations(conn, project_id)

    # 10. Code dependency edges — actual edges from import analysis
    summary["steps"]["code_deps"] = _extract_deps(conn, project_id)

    log.info("absorb: done for project=%s", project_id)
    return summary


def _update_co_citations(conn: sqlite3.Connection, project_id: str) -> dict:
    """Add semantic edges between interpretation nodes that cite the same raw.

    Safe to re-run: skips pairs that already have a co_occurs edge.
    """
    from loci.graph.models import new_id

    pairs = conn.execute("""
        SELECT DISTINCT e1.src AS a, e2.src AS b
        FROM edges e1 JOIN edges e2 ON e1.dst = e2.dst AND e1.src < e2.src
        WHERE e1.type = 'cites' AND e2.type = 'cites'
          AND e1.src IN (SELECT node_id FROM interpretation_nodes)
          AND e2.src IN (SELECT node_id FROM interpretation_nodes)
          AND e1.src IN (SELECT node_id FROM project_effective_members WHERE project_id = ?)
    """, (project_id,)).fetchall()

    added = 0
    for pair in pairs:
        a, b = pair[0], pair[1]
        if not conn.execute(
            "SELECT 1 FROM edges WHERE src=? AND dst=? AND type='semantic'", (a, b)
        ).fetchone():
            conn.execute(
                "INSERT INTO edges(id, src, dst, type, weight, created_by, created_at)"
                " VALUES (?,?,?,?,?,?,datetime('now'))",
                (new_id(), a, b, "semantic", 1.0, "system"),
            )
            added += 1
    if added:
        conn.commit()
    return {"added": added}


def _fs_audit(conn: sqlite3.Connection, project_id: str) -> dict:
    """Flip raw_nodes.source_of_truth based on whether the canonical_path still exists.

    Note: nodes shared across projects share `raw_nodes`; flipping the flag
    affects all projects that contain this raw. That's intentional — the file
    is missing globally, not just from this project's view.
    """
    rows = conn.execute(
        """
        SELECT r.node_id, r.canonical_path, r.source_of_truth
        FROM raw_nodes r
        JOIN project_effective_members pm ON pm.node_id = r.node_id
        WHERE pm.project_id = ?
        """,
        (project_id,),
    ).fetchall()
    flipped_to_missing = 0
    flipped_to_present = 0
    for r in rows:
        exists = Path(r["canonical_path"]).is_file()
        sot = bool(r["source_of_truth"])
        if sot and not exists:
            conn.execute(
                "UPDATE raw_nodes SET source_of_truth = 0 WHERE node_id = ?",
                (r["node_id"],),
            )
            flipped_to_missing += 1
        elif not sot and exists:
            conn.execute(
                "UPDATE raw_nodes SET source_of_truth = 1 WHERE node_id = ?",
                (r["node_id"],),
            )
            flipped_to_present += 1
    return {
        "checked": len(rows),
        "flipped_to_missing": flipped_to_missing,
        "flipped_to_present": flipped_to_present,
    }
