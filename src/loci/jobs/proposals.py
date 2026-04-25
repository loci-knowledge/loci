"""Proposal generation: alias detection, broken-support tensions, forgetting.

Each function appends rows to `proposals` (with a fingerprint to dedupe). The
absorb job calls them in sequence after the contradiction pass.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3

from loci.config import get_settings
from loci.graph.models import new_id, now_iso

log = logging.getLogger(__name__)

# Per PLAN.md §Edge cases: cosine similarity > 0.92 → propose `aliases`.
ALIAS_COSINE_THRESHOLD = 0.92


def _fingerprint(kind: str, payload: dict) -> str:
    """Deterministic fingerprint for dedupe. Sorted json hash."""
    canonical = json.dumps({"kind": kind, **payload}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def _insert_proposal(
    conn: sqlite3.Connection, project_id: str, kind: str, payload: dict,
) -> str | None:
    """Insert if not duplicate (UNIQUE on (project_id, fingerprint)). Returns id or None."""
    fp = _fingerprint(kind, payload)
    pid = new_id()
    try:
        conn.execute(
            """
            INSERT INTO proposals(id, project_id, kind, payload, status, fingerprint)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (pid, project_id, kind, json.dumps(payload), fp),
        )
        return pid
    except sqlite3.IntegrityError:
        return None  # duplicate


# ---------------------------------------------------------------------------
# Alias detection
# ---------------------------------------------------------------------------


def detect_aliases(
    conn: sqlite3.Connection, project_id: str,
    *, threshold: float = ALIAS_COSINE_THRESHOLD,
) -> list[str]:
    """For each interp pair with cosine > threshold, propose an `aliases` edge.

    Implementation: for each interp node in the project, ANN-search the top-2
    closest others (k=2 to skip self). If distance² < 2*(1-threshold) — i.e.
    cosine > threshold — file a proposal.

    Returns the list of new proposal ids.
    """
    new_proposals: list[str] = []
    # Pull every interp node id + its embedding blob in one query.
    rows = conn.execute(
        """
        SELECT n.id AS id, v.embedding AS emb
        FROM nodes n
        JOIN project_membership pm ON pm.node_id = n.id
        JOIN node_vec v ON v.node_id = n.id
        WHERE pm.project_id = ? AND pm.role != 'excluded'
          AND n.kind = 'interpretation' AND n.status IN ('live','dirty')
        """,
        (project_id,),
    ).fetchall()
    if len(rows) < 2:
        return []
    # Self ANN: for each row we'd ideally use the same `node_vec MATCH ?`
    # query. We do it node-by-node, k=3 (top match is itself, so we need 2-3
    # extras). Acceptable for thousands of interp nodes.
    for r in rows:
        emb = r["emb"]  # already a blob
        hits = conn.execute(
            """
            SELECT v.node_id AS node_id, v.distance AS distance
            FROM node_vec v
            JOIN nodes n ON n.id = v.node_id
            JOIN project_membership pm ON pm.node_id = n.id
            WHERE v.embedding MATCH ? AND k = 3
              AND pm.project_id = ? AND pm.role != 'excluded'
              AND n.kind = 'interpretation' AND n.status IN ('live','dirty')
              AND v.node_id != ?
            ORDER BY v.distance
            """,
            (emb, project_id, r["id"]),
        ).fetchall()
        for h in hits:
            cos = 1 - (h["distance"] ** 2) / 2
            if cos < threshold:
                continue
            a, b = sorted([r["id"], h["node_id"]])
            payload = {"a": a, "b": b, "similarity": round(float(cos), 4)}
            pid = _insert_proposal(conn, project_id, "alias", payload)
            if pid:
                new_proposals.append(pid)
    return new_proposals


# ---------------------------------------------------------------------------
# Broken-support tensions
# ---------------------------------------------------------------------------


def detect_broken_supports(conn: sqlite3.Connection, project_id: str) -> list[str]:
    """Find interp nodes whose `cites` raw nodes are no longer source-of-truth.

    For each such interp, file a `broken` proposal (PLAN.md §Edge cases (1)).
    Also marks the interp `stale` if it loses ALL supports.
    """
    out: list[str] = []
    # Interp nodes whose at least one cited raw is missing
    rows = conn.execute(
        """
        SELECT i.id AS interp_id, r.id AS raw_id
        FROM nodes i
        JOIN project_membership pm ON pm.node_id = i.id
        JOIN edges e ON e.src = i.id AND e.type = 'cites'
        JOIN nodes r ON r.id = e.dst
        JOIN raw_nodes rn ON rn.node_id = r.id
        WHERE pm.project_id = ? AND pm.role != 'excluded'
          AND i.kind = 'interpretation' AND i.status IN ('live','dirty')
          AND rn.source_of_truth = 0
        """,
        (project_id,),
    ).fetchall()
    affected_interps: dict[str, list[str]] = {}
    for r in rows:
        affected_interps.setdefault(r["interp_id"], []).append(r["raw_id"])
    for interp_id, missing in affected_interps.items():
        for raw_id in missing:
            payload = {"about_node_id": interp_id, "missing_raw_id": raw_id}
            pid = _insert_proposal(conn, project_id, "broken", payload)
            if pid:
                out.append(pid)
        # If ALL cites raws are missing, mark stale.
        all_cites = conn.execute(
            "SELECT COUNT(*) AS c FROM edges WHERE src = ? AND type = 'cites'",
            (interp_id,),
        ).fetchone()["c"]
        if all_cites == len(missing):
            conn.execute(
                "UPDATE nodes SET status = 'stale', updated_at = ? WHERE id = ?",
                (now_iso(), interp_id),
            )
    return out


# ---------------------------------------------------------------------------
# Forgetting
# ---------------------------------------------------------------------------


def detect_forgetting_candidates(conn: sqlite3.Connection, project_id: str) -> list[str]:
    """Surface low-access, low-confidence interp nodes as dismissal proposals.

    PLAN.md §Cost model: nodes with access_count == 0 over N days *and*
    confidence < floor become dismissed candidates. We surface them as
    proposals (kind='node' with action='dismiss') — never auto-delete.
    """
    settings = get_settings()
    cutoff_days = settings.forgetting_inactivity_days
    floor = settings.forgetting_confidence_floor
    out: list[str] = []
    rows = conn.execute(
        """
        SELECT n.id, n.title, n.confidence, n.last_accessed_at
        FROM nodes n
        JOIN project_membership pm ON pm.node_id = n.id
        WHERE pm.project_id = ? AND pm.role != 'excluded'
          AND n.kind = 'interpretation'
          AND n.status IN ('live','dirty')
          AND n.access_count = 0
          AND n.confidence < ?
          AND (n.last_accessed_at IS NULL
               OR julianday('now') - julianday(n.last_accessed_at) > ?)
        """,
        (project_id, floor, cutoff_days),
    ).fetchall()
    for r in rows:
        payload = {
            "about_node_id": r["id"], "action": "dismiss",
            "title": r["title"],
            "reason": f"low confidence + no access in {cutoff_days} days",
        }
        pid = _insert_proposal(conn, project_id, "node", payload)
        if pid:
            out.append(pid)
    return out
