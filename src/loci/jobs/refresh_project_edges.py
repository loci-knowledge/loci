"""refresh_project_edges job — recompute project-scoped co_aspect edges for a resource.

Cheap, no-LLM. Finds all other resources in the same project that share at
least one project aspect with the target resource and writes weighted co_aspect
edges proportional to the overlap fraction.

Payload shape:
    {
      "resource_id": "<ULID>"
    }

`project_id` is taken from `job["project_id"]`.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from collections import defaultdict
from itertools import combinations

log = logging.getLogger(__name__)


async def handle_refresh_project_edges(
    job: dict, conn: sqlite3.Connection, settings
) -> dict:
    """Recompute project-scoped co_aspect edges for a single resource.

    Steps:
    1. Get all project_resource_aspects for this (project, resource).
    2. Find other project members that share ≥ 1 aspect.
    3. Write weighted co_aspect edges (project-scoped).
    """
    payload = job.get("payload", {})
    resource_id = payload.get("resource_id")
    project_id = job.get("project_id")

    if not resource_id:
        log.warning("refresh_project_edges: payload missing resource_id")
        return {"skipped": "missing_resource_id"}
    if not project_id:
        log.warning("refresh_project_edges: job missing project_id")
        return {"skipped": "missing_project_id"}

    # 1. Collect aspect_ids for this (project, resource).
    aspect_rows = conn.execute(
        """
        SELECT aspect_id
        FROM project_resource_aspects
        WHERE project_id = ? AND resource_id = ?
        """,
        (project_id, resource_id),
    ).fetchall()

    my_aspect_ids = [r["aspect_id"] for r in aspect_rows]

    if not my_aspect_ids:
        log.info(
            "refresh_project_edges: resource=%s has no project aspects; nothing to do",
            resource_id,
        )
        return {"edges_written": 0}

    # 2. Find other resources in the project that share ≥ 1 aspect.
    placeholders = ",".join("?" * len(my_aspect_ids))
    neighbor_rows = conn.execute(
        f"""
        SELECT pra2.resource_id, COUNT(*) AS shared
        FROM project_resource_aspects pra2
        JOIN project_effective_members pem ON pem.node_id = pra2.resource_id
        WHERE pem.project_id = ?
          AND pra2.resource_id != ?
          AND pra2.aspect_id IN ({placeholders})
        GROUP BY pra2.resource_id
        """,
        (project_id, resource_id, *my_aspect_ids),
    ).fetchall()

    if not neighbor_rows:
        return {"edges_written": 0}

    my_count = len(my_aspect_ids)
    edges_written = 0

    for nb_row in neighbor_rows:
        neighbor_id = nb_row["resource_id"]
        shared = nb_row["shared"]
        weight = min(1.0, shared / max(my_count, 1))

        _upsert_project_edge(
            conn,
            src_id=resource_id,
            dst_id=neighbor_id,
            edge_type="co_aspect",
            weight=weight,
            project_id=project_id,
        )
        edges_written += 1

    log.info(
        "refresh_project_edges: resource=%s project=%s edges_written=%d",
        resource_id,
        project_id,
        edges_written,
    )

    # 4. Compute PMI edges for the whole project.
    pmi_written = _compute_pmi_edges(conn, project_id)
    log.info(
        "refresh_project_edges: project=%s pmi_edges_written=%d",
        project_id,
        pmi_written,
    )

    return {"edges_written": edges_written, "pmi_edges_written": pmi_written}


def _compute_pmi_edges(conn: sqlite3.Connection, project_id: str) -> int:
    """Compute PMI-weighted co-occurrence edges between aspects within a project.

    Algorithm:
    1. Fetch all (resource_id, aspect_id) pairs for the project from
       project_resource_aspects.
    2. Compute per-aspect counts and pairwise co-occurrence counts.
    3. For each pair (a, b) with co-occurrence count >= 2:
       pmi = log( P(a,b) / (P(a) * P(b)) )
       Write as aspect_edges row with edge_type='co_aspect_pmi', weight=pmi
       when pmi > 0.

    Skips gracefully if the aspect_edges table does not exist yet.

    Returns the number of PMI edges written/updated.
    """
    # Guard: check that aspect_edges exists.
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='aspect_edges'"
        )
    }
    if "aspect_edges" not in tables:
        log.debug("_compute_pmi_edges: aspect_edges table not present; skipping")
        return 0

    rows = conn.execute(
        """
        SELECT resource_id, aspect_id
        FROM project_resource_aspects
        WHERE project_id = ?
        """,
        (project_id,),
    ).fetchall()

    if not rows:
        return 0

    # Group aspect_ids by resource so we can compute co-occurrence.
    resource_to_aspects: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        resource_to_aspects[row["resource_id"]].append(row["aspect_id"])

    N = len(resource_to_aspects)  # number of "documents"
    if N == 0:
        return 0

    # Count per-aspect occurrences (in how many resources).
    count_a: dict[str, int] = defaultdict(int)
    # Count co-occurrence (in how many resources do both appear).
    count_ab: dict[tuple[str, str], int] = defaultdict(int)

    for aspect_list in resource_to_aspects.values():
        unique_aspects = list(set(aspect_list))
        for asp in unique_aspects:
            count_a[asp] += 1
        for a, b in combinations(sorted(unique_aspects), 2):
            count_ab[(a, b)] += 1

    from loci.graph.aspect_graph import AspectGraphRepository
    ag_repo = AspectGraphRepository(conn)
    written = 0

    for (a, b), count in count_ab.items():
        if count < 2:
            continue
        p_a = count_a[a] / N
        p_b = count_a[b] / N
        p_ab = count / N
        pmi = math.log(p_ab / (p_a * p_b))
        if pmi <= 0:
            continue
        try:
            ag_repo.add_edge(
                src_aspect_id=a,
                dst_aspect_id=b,
                edge_type="co_aspect_pmi",
                weight=pmi,
                project_id=project_id,
            )
            written += 1
        except Exception:  # noqa: BLE001
            log.warning(
                "_compute_pmi_edges: failed to write edge (%s, %s); skipping",
                a,
                b,
                exc_info=True,
            )

    return written


def _upsert_project_edge(
    conn: sqlite3.Connection,
    *,
    src_id: str,
    dst_id: str,
    edge_type: str,
    weight: float,
    project_id: str,
) -> None:
    """Upsert a project-scoped concept edge.

    Idempotent on (src_id, dst_id, edge_type, project_id).
    """
    from loci.graph.models import new_id, now_iso

    existing = conn.execute(
        """
        SELECT id FROM concept_edges
        WHERE src_id = ? AND dst_id = ? AND edge_type = ?
          AND project_id IS ?
        """,
        (src_id, dst_id, edge_type, project_id),
    ).fetchone()

    if existing is not None:
        conn.execute(
            """
            UPDATE concept_edges
            SET weight = ?
            WHERE src_id = ? AND dst_id = ? AND edge_type = ?
              AND project_id IS ?
            """,
            (weight, src_id, dst_id, edge_type, project_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO concept_edges(
                id, src_id, dst_id, edge_type,
                relation_hint, weight, metadata, project_id, created_at
            )
            VALUES (?, ?, ?, ?, NULL, ?, NULL, ?, ?)
            """,
            (new_id(), src_id, dst_id, edge_type, weight, project_id, now_iso()),
        )
