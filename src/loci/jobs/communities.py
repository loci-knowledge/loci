"""Leiden community detection (optional — gated on igraph).

PLAN.md §Inspiration: GraphRAG-style hierarchical communities. We compute one
snapshot per absorb run when the user has installed `loci[graph]` (igraph +
leidenalg). Without those packages we no-op gracefully.

The communities feed back into retrieval as `co_occurs` edges (if you're in
the same community as a high-PPR node, your PPR mass goes up).
"""

from __future__ import annotations

import json
import logging
import sqlite3

from loci.graph.models import new_id, now_iso

log = logging.getLogger(__name__)


def run(conn: sqlite3.Connection, project_id: str) -> dict:
    try:
        import igraph as ig  # type: ignore[import-not-found]
        import leidenalg  # type: ignore[import-not-found]
    except ImportError:
        log.info("communities: igraph/leidenalg not installed; skipping")
        return {"skipped": True, "reason": "no_igraph"}

    # Pull interp nodes + their edges into igraph format.
    nodes = conn.execute(
        """
        SELECT n.id FROM nodes n
        JOIN project_membership pm ON pm.node_id = n.id
        WHERE pm.project_id = ? AND pm.role != 'excluded'
          AND n.kind = 'interpretation' AND n.status IN ('live','dirty')
        """,
        (project_id,),
    ).fetchall()
    if len(nodes) < 4:
        return {"skipped": True, "reason": "too_few_nodes"}
    node_ids = [r["id"] for r in nodes]
    index = {nid: i for i, nid in enumerate(node_ids)}

    edge_rows = conn.execute(
        """
        SELECT src, dst, weight
        FROM edges
        WHERE type IN ('reinforces','extends','specializes','generalizes','co_occurs','aliases')
          AND src IN ({0}) AND dst IN ({0})
        """.format(",".join("?" * len(node_ids))),
        (*node_ids, *node_ids),
    ).fetchall()

    edges = [(index[r["src"]], index[r["dst"]]) for r in edge_rows]
    weights = [r["weight"] for r in edge_rows]
    g = ig.Graph(n=len(node_ids), edges=edges, directed=False)
    g.es["weight"] = weights

    partition = leidenalg.find_partition(
        g, leidenalg.RBConfigurationVertexPartition,
        weights="weight", n_iterations=-1,
    )

    # Persist a snapshot.
    snap_at = now_iso()
    inserted = 0
    for level, members in enumerate(partition):
        members_list = [node_ids[i] for i in members]
        if len(members_list) < 2:
            continue
        conn.execute(
            """
            INSERT INTO communities(id, project_id, snapshot_at, level, label, member_node_ids)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id(), project_id, snap_at, level, None, json.dumps(members_list)),
        )
        inserted += 1
    return {"snapshot_at": snap_at, "communities": inserted, "modularity": partition.modularity}
