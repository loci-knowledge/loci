"""Absorb job — the checkpoint pipeline.

Steps run in dependency order:

    1. validate filesystem (raw source_of_truth audit)
    2. replay traces → access_count, confidence, last_accessed_at
    3. detect_orphans (flip orphans to dirty)
    4. detect_broken_supports (mark stale, file proposals)
    5. detect_aliases (cosine > 0.92 → propose `aliases`)
    6. detect_forgetting_candidates (low conf + no access → propose dismiss)
    7. contradiction_pass (LLM-mediated; gated on Anthropic key)
    8. communities (igraph/leidenalg; gated on optional dep)

Co-citation and code-dependency passes are gone with the DAG migration:
co-citation produced symmetric semantic edges (cycles), and code-deps produced
raw→raw `actual` edges (broke the raw-leaf rule). The DAG model expresses these
relationships as overlapping `cites` fan-outs from interpretation nodes.

Each step returns a summary dict; we collate them all into the job result.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

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

    # 8. Communities (igraph-gated) — over the derives_from interp graph
    summary["steps"]["communities"] = communities.run(conn, project_id)

    log.info("absorb: done for project=%s", project_id)
    return summary


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
