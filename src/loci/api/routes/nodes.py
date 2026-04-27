"""Node endpoints.

PLAN.md §API §Graph manipulation:

    POST /nodes                      create (usually proposed)
    GET  /nodes/:id                  full node + edges + raw supports
    PATCH /nodes/:id                 edit body / title / tags / status
    POST /nodes/:id/accept           proposed → live
    POST /nodes/:id/dismiss          → dismissed
    POST /nodes/:id/pin              role: pinned in current project
    GET  /nodes/:id/trace            session history of this node
    GET  /nodes/:id/responses        responses that cited this node

After-DB-commit, every mutation (create / edit / accept / dismiss / pin)
publishes a graph-delta event to `project:{id}` so the VSCode extension's
GraphSocket can update the visualisation in real time. The payload mirrors
the shape `loki-frontend/extension/src/state/deltaReducer.ts` already accepts:
`{op: "upsert", entity: "node", payload: <node-dict>, seq, ts}`.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from loci.api.dependencies import db
from loci.api.publishers import publish_node_delete, publish_node_upsert
from loci.citations import CitationTracker
from loci.embed.local import get_embedder
from loci.graph import EdgeRepository, NodeRepository, ProjectRepository
from loci.graph.models import (
    InterpretationNode,
    InterpretationOrigin,
    InterpretationSubkind,
    NodeStatus,
)

router = APIRouter(tags=["nodes"])


class CreateNode(BaseModel):
    """Create an interpretation node (raw nodes are created via ingest only)."""
    project_id: str
    subkind: InterpretationSubkind
    title: str
    body: str = ""
    tags: list[str] = Field(default_factory=list)
    origin: InterpretationOrigin = "user_explicit_create"
    origin_session_id: str | None = None
    origin_response_id: str | None = None
    status: NodeStatus = "live"
    confidence: float = 0.7
    # Optional links to wire up at creation time.
    cites: list[str] = Field(default_factory=list)        # raw node ids
    related: dict[str, list[str]] = Field(default_factory=dict)
    # related: { "reinforces": [...], "extends": [...], ... }


class PatchNode(BaseModel):
    title: str | None = None
    body: str | None = None
    tags: list[str] | None = None
    status: NodeStatus | None = None


class PatchLocus(BaseModel):
    relation_md: str | None = None
    overlap_md: str | None = None
    source_anchor_md: str | None = None
    angle: str | None = None


@router.post("/nodes", status_code=201)
def create_node(
    body: CreateNode, conn: sqlite3.Connection = Depends(db),
) -> dict:
    from loci.api.publishers import publish_edge_upsert

    nodes_repo = NodeRepository(conn)
    edges_repo = EdgeRepository(conn)
    projects_repo = ProjectRepository(conn)
    if projects_repo.get(body.project_id) is None:
        raise HTTPException(404, detail="project not found")

    n = InterpretationNode(
        subkind=body.subkind, title=body.title, body=body.body,
        tags=body.tags, origin=body.origin,
        origin_session_id=body.origin_session_id,
        origin_response_id=body.origin_response_id,
        status=body.status, confidence=body.confidence,
    )
    # Embed the body so the node is searchable immediately. Title prepended
    # for the same reason as in ingest (it's high signal).
    text = f"{n.title}\n\n{n.body}".strip()
    emb = None
    if text:
        emb = get_embedder().encode(text)
    nodes_repo.create_interpretation(n, embedding=emb)
    projects_repo.add_member(body.project_id, n.id, role="included")

    edges_created: list[str] = []
    new_edges = []
    for raw_id in body.cites:
        for e in edges_repo.create(n.id, raw_id, type="cites"):
            edges_created.append(e.id)
            new_edges.append(e)
    for typ, dst_ids in body.related.items():
        for dst in dst_ids:
            for e in edges_repo.create(n.id, dst, type=typ):  # type: ignore[arg-type]
                edges_created.append(e.id)
                new_edges.append(e)

    # Publish after-commit (the repos run their own transactions). One node
    # upsert + one edge upsert per created edge. The project_id is fixed
    # because we just added the membership above.
    publish_node_upsert(conn, n, project_ids=[body.project_id])
    for e in new_edges:
        publish_edge_upsert(conn, e, project_ids=[body.project_id])
    return {"node_id": n.id, "edges_created": edges_created}


@router.get("/nodes/{node_id}")
def get_node(
    node_id: str, conn: sqlite3.Connection = Depends(db),
) -> dict:
    nodes_repo = NodeRepository(conn)
    edges_repo = EdgeRepository(conn)
    n = nodes_repo.get(node_id)
    if n is None:
        raise HTTPException(404, detail="node not found")
    out_edges = edges_repo.from_node(node_id)
    in_edges = [
        edges_repo._row_to_edge(r)
        for r in conn.execute("SELECT * FROM edges WHERE dst = ?", (node_id,)).fetchall()
    ]
    return {
        "node": n.model_dump(),
        "edges_out": [e.model_dump() for e in out_edges],
        "edges_in": [e.model_dump() for e in in_edges],
    }


@router.patch("/nodes/{node_id}")
def patch_node(
    node_id: str, body: PatchNode,
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    nodes_repo = NodeRepository(conn)
    n = nodes_repo.get(node_id)
    if n is None:
        raise HTTPException(404, detail="node not found")
    new_emb = None
    if body.body is not None:
        text = f"{body.title or n.title}\n\n{body.body}".strip()
        if text:
            new_emb = get_embedder().encode(text)
    nodes_repo.update_body(
        node_id,
        title=body.title, body=body.body, tags=body.tags,
        new_embedding=new_emb,
    )
    if body.status is not None:
        nodes_repo.set_status(node_id, body.status)
    # Re-fetch and publish so subscribers get the updated shape.
    updated = nodes_repo.get(node_id)
    if updated is not None:
        publish_node_upsert(conn, updated)
    return {"updated": True}


@router.post("/nodes/{node_id}/accept")
def accept_node(
    node_id: str, conn: sqlite3.Connection = Depends(db),
) -> dict:
    nodes_repo = NodeRepository(conn)
    n = nodes_repo.get(node_id)
    if n is None:
        raise HTTPException(404, detail="node not found")
    nodes_repo.set_status(node_id, "live")
    nodes_repo.bump_confidence(node_id, +0.15)  # PLAN.md §Interaction vocabulary
    updated = nodes_repo.get(node_id)
    if updated is not None:
        publish_node_upsert(conn, updated)
    return {"status": "live"}


@router.post("/nodes/{node_id}/dismiss")
def dismiss_node(
    node_id: str, conn: sqlite3.Connection = Depends(db),
) -> dict:
    nodes_repo = NodeRepository(conn)
    if nodes_repo.get(node_id) is None:
        raise HTTPException(404, detail="node not found")
    nodes_repo.set_status(node_id, "dismissed")
    updated = nodes_repo.get(node_id)
    if updated is not None:
        publish_node_upsert(conn, updated)
    return {"status": "dismissed"}


@router.post("/nodes/{node_id}/pin")
def pin_node(
    node_id: str,
    project_id: str = Query(...),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    projects_repo = ProjectRepository(conn)
    if projects_repo.get(project_id) is None:
        raise HTTPException(404, detail="project not found")
    if NodeRepository(conn).get(node_id) is None:
        raise HTTPException(404, detail="node not found")
    projects_repo.add_member(project_id, node_id, role="pinned")
    CitationTracker(conn).append_trace(project_id, node_id, "pinned")
    updated = NodeRepository(conn).get(node_id)
    if updated is not None:
        # Pin only mutates membership in this project; the node itself is
        # unchanged, but we re-publish so clients see the new role.
        publish_node_upsert(conn, updated, project_ids=[project_id])
    return {"role": "pinned"}


@router.get("/nodes/{node_id}/trace")
def get_node_trace(
    node_id: str,
    limit: int = Query(200, le=1000),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    return {"traces": CitationTracker(conn).trace_for_node(node_id, limit=limit)}


@router.get("/nodes/{node_id}/responses")
def get_node_responses(
    node_id: str,
    limit: int = Query(50, le=500),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    return {"responses": CitationTracker(conn).responses_citing_node(node_id, limit=limit)}


@router.patch("/nodes/{node_id}/locus")
def patch_node_locus(
    node_id: str,
    body: PatchLocus,
    if_match: str | None = Header(None, alias="If-Match"),
    x_loci_actor: str | None = Header(None, alias="X-Loci-Actor"),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    from loci.api.publishers import projects_for_node
    nodes_repo = NodeRepository(conn)
    n = nodes_repo.get(node_id)
    if n is None:
        raise HTTPException(404, detail="node not found")
    if n.kind != "interpretation":
        raise HTTPException(400, detail="locus patch only applies to interpretation nodes")
    if if_match is not None and n.updated_at != if_match:
        raise HTTPException(409, detail="conflict: node modified since If-Match value was read")
    actor = x_loci_actor if x_loci_actor in ("user", "agent", "system") else "user"
    # Re-embed using the merged (old + new) slot text.
    rel = body.relation_md if body.relation_md is not None else getattr(n, "relation_md", "")
    ovl = body.overlap_md if body.overlap_md is not None else getattr(n, "overlap_md", "")
    anc = body.source_anchor_md if body.source_anchor_md is not None else getattr(n, "source_anchor_md", "")
    emb_text = "\n\n".join(p for p in [n.title, rel, ovl, anc] if p).strip()
    new_emb = get_embedder().encode(emb_text) if emb_text else None
    nodes_repo.update_locus(
        node_id,
        relation_md=body.relation_md, overlap_md=body.overlap_md,
        source_anchor_md=body.source_anchor_md, angle=body.angle,
        new_embedding=new_emb,
        actor=actor, source_tool="api.patch_node_locus",
    )
    pids = projects_for_node(conn, node_id)
    if pids:
        CitationTracker(conn).append_trace(pids[0], node_id, "edited")
    updated = nodes_repo.get(node_id)
    if updated is not None:
        publish_node_upsert(conn, updated)
    return {"updated": True}


@router.delete("/nodes/{node_id}")
def delete_node(
    node_id: str, conn: sqlite3.Connection = Depends(db),
) -> dict:
    from loci.api.publishers import (
        projects_for_edge,
        projects_for_node,
        publish_edge_delete,
    )
    nodes_repo = NodeRepository(conn)
    n = nodes_repo.get(node_id)
    if n is None:
        raise HTTPException(404, detail="node not found")
    if n.kind == "raw":
        raise HTTPException(400, detail="raw nodes cannot be deleted via this endpoint")
    # Snapshot edge fan-out BEFORE deletion (rows are gone after hard_delete).
    edge_rows = conn.execute(
        "SELECT id, src, dst FROM edges WHERE src = ? OR dst = ?", (node_id, node_id)
    ).fetchall()
    edge_fan = [
        (r["id"], r["src"], r["dst"], projects_for_edge(conn, r["src"], r["dst"]))
        for r in edge_rows
    ]
    node_project_ids = projects_for_node(conn, node_id)
    nodes_repo.hard_delete(node_id, actor="user", source_tool="api.delete_node")
    for eid, src, dst, pids in edge_fan:
        publish_edge_delete(conn, eid, src=src, dst=dst, project_ids=pids)
    publish_node_delete(conn, node_id, project_ids=node_project_ids)
    return {"deleted": True}
