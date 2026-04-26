"""Retrieve endpoint.

    POST /projects/:id/retrieve
      body: { query, k, anchors?, include?, hyde? }
      returns:
        {
          nodes:           [...]   # ranked raws (default) or filtered set
          routing_loci:    [...]   # the loci that routed to those raws
          trace_table:     [...]   # per-raw interp path
          trace_id:        str
        }

The default `include` is raws only. To surface loci themselves (e.g. for graph
inspection or debugging), pass include=["interpretation"] explicitly.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

from loci.api.dependencies import db, project_by_id
from loci.api.publishers import publish_trace_run
from loci.citations import CitationTracker, ResponseRecord
from loci.graph.models import Project, now_iso
from loci.retrieve import RetrievalRequest, Retriever

router = APIRouter(prefix="/projects", tags=["retrieve"])


class RetrieveBody(BaseModel):
    query: str
    k: int = 10
    anchors: list[str] | None = None
    include: list[str] | None = None
    hyde: bool = False
    session_id: str = "default"


class RetrieveNodeOut(BaseModel):
    id: str
    kind: str
    subkind: str
    title: str
    snippet: str
    score: float
    why: str
    # Per-node interp trace: list of {id, edge, to} hops.
    trace: list[dict] = []


class RoutingLocusOut(BaseModel):
    id: str
    subkind: str
    title: str
    relation_md: str
    overlap_md: str
    source_anchor_md: str
    angle: str | None
    score: float


class RetrieveResponseBody(BaseModel):
    nodes: list[RetrieveNodeOut]
    routing_loci: list[RoutingLocusOut]
    trace_table: list[dict]
    trace_id: str


@router.post("/{project_id}/retrieve")
def post_retrieve(
    body: RetrieveBody,
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
    user_agent: str = Header("unknown"),
) -> RetrieveResponseBody:
    from loci.api.routes.anchors import get_active_anchors

    anchors = (
        get_active_anchors(project.id) if body.anchors is None
        else list(body.anchors)
    )

    req = RetrievalRequest(
        project_id=project.id,
        query=body.query,
        k=body.k,
        anchors=anchors,
        include=body.include,
        hyde=body.hyde,
    )
    resp = Retriever(conn).retrieve(req)
    record = ResponseRecord(
        project_id=project.id, session_id=body.session_id,
        request=body.model_dump(),
        output="",
        cited_node_ids=[],
        trace_table=resp.trace_table,
        client=user_agent,
    )
    rid = CitationTracker(conn).write_response(
        record, retrieved_node_ids=[n.node_id for n in resp.nodes],
    )
    publish_trace_run(
        project.id,
        response_id=rid,
        session_id=body.session_id,
        query=body.query,
        ts=now_iso(),
        routing_loci=[
            {
                "id": ri.node_id, "subkind": ri.subkind, "title": ri.title,
                "relation_md": ri.relation_md, "overlap_md": ri.overlap_md,
                "source_anchor_md": ri.source_anchor_md,
                "angle": ri.angle, "score": ri.score,
            }
            for ri in resp.routing_interps
        ],
        trace_table=resp.trace_table,
        k=body.k,
    )
    return RetrieveResponseBody(
        nodes=[
            RetrieveNodeOut(
                id=n.node_id, kind=n.kind, subkind=n.subkind, title=n.title,
                snippet=n.snippet, score=n.score, why=n.why,
                trace=[
                    {"id": h.src, "edge": h.edge_type, "to": h.dst}
                    for h in n.trace
                ],
            )
            for n in resp.nodes
        ],
        routing_loci=[
            RoutingLocusOut(
                id=ri.node_id, subkind=ri.subkind, title=ri.title,
                relation_md=ri.relation_md, overlap_md=ri.overlap_md,
                source_anchor_md=ri.source_anchor_md, angle=ri.angle,
                score=ri.score,
            )
            for ri in resp.routing_interps
        ],
        trace_table=resp.trace_table,
        trace_id=rid,
    )


class TraceRunBroadcastBody(BaseModel):
    response_id: str
    session_id: str | None = None
    query: str
    ts: str
    k: int = 10
    routing_loci: list[dict]
    trace_table: list[dict]
    truncated_rows: int = 0


@router.post("/{project_id}/mcp/publish-trace", status_code=204)
def post_publish_trace(
    body: TraceRunBroadcastBody,
    project: Project = Depends(project_by_id),
) -> None:
    """Accept a trace-run payload from the MCP server and publish it to the WS bus.

    The MCP server runs in a separate process and cannot reach the in-process
    pub/sub bus directly. This endpoint bridges the gap: the MCP tool calls
    this endpoint fire-and-forget after retrieve/draft so the frontend receives
    the structured trace for the village highlight.
    """
    publish_trace_run(
        project.id,
        response_id=body.response_id,
        session_id=body.session_id,
        query=body.query,
        ts=body.ts,
        routing_loci=body.routing_loci,
        trace_table=body.trace_table,
        k=body.k,
    )
