"""Personalised PageRank over the interpretation graph.

Reference: HippoRAG 2 (arXiv 2502.14802) for the dual-node + PPR pattern. We
implement only the PPR step here; the dual-node split is encoded in the schema
(raw vs interpretation nodes) and the orchestrator decides what to feed in.

The graph: nodes are *interpretation* node ids (raw nodes are leaves and don't
participate in PPR). Edges are weighted; we use all interp↔interp edge types
(reinforces, contradicts, extends, specializes, generalizes, aliases,
co_occurs) with their weights. Direction matters — PPR walks the directed
graph as-is. Symmetric edge types already have reciprocal rows in the table
(see `loci/graph/edges.py`), so the random walk can move both ways.

Math:

    M = column-normalised weighted adjacency       (out-edges)
    p = personalisation vector (1/|anchors| for anchors, 0 otherwise)
    r₀ = p
    r_{t+1} = (1-α)·p + α·M·r_t

Converges in ~20-50 iterations at α=0.85. We scale linearly with the number
of edges (one sparse matrix-vector multiply per iteration), so 50k nodes with
500k edges runs in <50ms on a laptop.

Sparse layout: `scipy.sparse.csr_matrix`. Rows are dst, columns are src
(transposed adjacency), so M @ r computes "for each dst, sum over inbound
edges of weight × source mass".
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np
from scipy.sparse import csr_matrix

from loci.config import get_settings

# Edge types that participate in the random walk. `cites` is excluded because
# PPR runs over interpretation nodes only; cites is interp→raw and would
# create dangling targets.
PPR_EDGE_TYPES: tuple[str, ...] = (
    "reinforces", "contradicts", "extends",
    "specializes", "generalizes", "aliases", "co_occurs",
)


@dataclass
class PPRResult:
    # Mapping node_id → score (∈ [0, 1] approximately, sums to 1 over all nodes).
    scores: dict[str, float]
    # The set of anchor node ids actually used (after filtering to interp nodes
    # that exist in this project).
    anchors_used: list[str]
    # Iteration count taken to converge.
    iterations: int


def run(
    conn: sqlite3.Connection,
    project_id: str,
    anchor_ids: list[str],
    *,
    alpha: float | None = None,
    max_iter: int | None = None,
    tol: float | None = None,
) -> PPRResult:
    """Run PPR on the interp subgraph of `project_id`, seeded by `anchor_ids`.

    Returns scores for every interpretation node that participates in this
    project. Nodes with no edges still appear (with their personalisation
    mass, if they're anchors; else 0 → omitted from `scores`).

    Anchors that aren't interpretation nodes in this project are silently
    dropped. If no valid anchors remain, returns an empty PPRResult — the
    caller is responsible for falling back to a no-PPR path.
    """
    settings = get_settings()
    alpha = alpha if alpha is not None else settings.ppr_alpha
    max_iter = max_iter if max_iter is not None else settings.ppr_max_iter
    tol = tol if tol is not None else settings.ppr_tol

    node_ids, M = _build_adjacency(conn, project_id)
    if not node_ids:
        return PPRResult(scores={}, anchors_used=[], iterations=0)
    index = {nid: i for i, nid in enumerate(node_ids)}

    valid_anchors = [a for a in anchor_ids if a in index]
    if not valid_anchors:
        return PPRResult(scores={}, anchors_used=[], iterations=0)

    p = np.zeros(len(node_ids), dtype=np.float64)
    for a in valid_anchors:
        p[index[a]] = 1.0
    p /= p.sum()  # safe: at least one anchor mapped

    r = p.copy()
    iterations = 0
    for it in range(1, max_iter + 1):
        r_new = (1 - alpha) * p + alpha * (M @ r)
        # Re-normalise: the walk is on a non-stochastic matrix because dangling
        # nodes (no out-edges) leak mass. We restore total mass each iteration.
        s = r_new.sum()
        if s > 0:
            r_new /= s
        if np.abs(r_new - r).sum() < tol:
            r = r_new
            iterations = it
            break
        r = r_new
        iterations = it

    # Pack non-zero scores back to node ids.
    nz = np.where(r > 1e-9)[0]
    scores = {node_ids[i]: float(r[i]) for i in nz}
    return PPRResult(scores=scores, anchors_used=valid_anchors, iterations=iterations)


def _build_adjacency(
    conn: sqlite3.Connection,
    project_id: str,
) -> tuple[list[str], csr_matrix]:
    """Return (node_ids, M) where M is column-normalised weighted adjacency.

    M[i, j] = weight(j → i) / out_degree_weight(j)

    So `M @ r` computes inbound weighted aggregation. Excludes raw nodes and
    nodes outside this project.
    """
    type_placeholders = ",".join("?" * len(PPR_EDGE_TYPES))
    # Get all interp nodes in this project.
    rows = conn.execute(
        """
        SELECT n.id AS id
        FROM nodes n
        JOIN project_membership pm ON pm.node_id = n.id
        WHERE pm.project_id = ?
          AND pm.role != 'excluded'
          AND n.kind = 'interpretation'
          AND n.status IN ('live','dirty')
        """,
        (project_id,),
    ).fetchall()
    node_ids = [r["id"] for r in rows]
    if not node_ids:
        return [], csr_matrix((0, 0), dtype=np.float64)
    index = {nid: i for i, nid in enumerate(node_ids)}

    # Edges among the interp nodes in this project.
    edge_rows = conn.execute(
        f"""
        SELECT e.src AS src, e.dst AS dst, e.weight AS weight
        FROM edges e
        WHERE e.type IN ({type_placeholders})
          AND e.src IN (SELECT node_id FROM project_membership
                        WHERE project_id = ? AND role != 'excluded')
          AND e.dst IN (SELECT node_id FROM project_membership
                        WHERE project_id = ? AND role != 'excluded')
        """,
        (*PPR_EDGE_TYPES, project_id, project_id),
    ).fetchall()

    n = len(node_ids)
    if not edge_rows:
        return node_ids, csr_matrix((n, n), dtype=np.float64)

    rows_idx: list[int] = []
    cols_idx: list[int] = []
    data: list[float] = []
    out_weight: dict[int, float] = {}
    for er in edge_rows:
        src_i = index.get(er["src"])
        dst_i = index.get(er["dst"])
        if src_i is None or dst_i is None:
            continue
        w = er["weight"]
        rows_idx.append(dst_i)
        cols_idx.append(src_i)
        data.append(w)
        out_weight[src_i] = out_weight.get(src_i, 0.0) + w

    arr_data = np.asarray(data, dtype=np.float64)
    # Normalise each column by its source's out-degree weight.
    for k, src_i in enumerate(cols_idx):
        ow = out_weight[src_i]
        if ow > 0:
            arr_data[k] /= ow

    M = csr_matrix(
        (arr_data, (np.asarray(rows_idx), np.asarray(cols_idx))),
        shape=(n, n),
        dtype=np.float64,
    )
    return node_ids, M
