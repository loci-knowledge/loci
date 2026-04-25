"""Retrieve endpoint.

PLAN.md §API §Retrieval:

    POST /projects/:id/retrieve
      body: { query, k, anchors?, include?, hyde? }
      returns: { nodes, citations, trace_id }
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

from loci.api.dependencies import db, project_by_id
from loci.citations import CitationTracker, ResponseRecord
from loci.graph.models import Project
from loci.retrieve import RetrievalRequest, Retriever

router = APIRouter(prefix="/projects", tags=["retrieve"])


class RetrieveBody(BaseModel):
    query: str
    k: int = 10
    # `anchors` is intentionally Optional so we can distinguish "not provided"
    # (None → fall back to the project's active-anchor set, if any) from
    # "explicitly empty" ([] → caller wants no anchors at all). This matches
    # PLAN.md §Retrieval semantics.
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


class CitationOut(BaseModel):
    node_id: str
    contributing_score: float
    edges_traversed: list[str]


class RetrieveResponseBody(BaseModel):
    nodes: list[RetrieveNodeOut]
    citations: list[CitationOut]
    trace_id: str  # = response_id; named per PLAN.md


@router.post("/{project_id}/retrieve")
def post_retrieve(
    body: RetrieveBody,
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
    user_agent: str = Header("unknown"),
) -> RetrieveResponseBody:
    # Anchor fallback: if the client didn't pass `anchors` at all, consult
    # the project's active-anchor set (frontend's "Pin for Claude Code"). An
    # explicit empty list is preserved verbatim — the caller wants none.
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
    # Persist a Response (with no output text — retrieve has no synthesised
    # output) and traces for everything we surfaced.
    record = ResponseRecord(
        project_id=project.id, session_id=body.session_id,
        request=body.model_dump(),
        output="",
        cited_node_ids=[],  # nothing was *cited*; just retrieved
        client=user_agent,
    )
    rid = CitationTracker(conn).write_response(
        record, retrieved_node_ids=[n.node_id for n in resp.nodes],
    )
    return RetrieveResponseBody(
        nodes=[
            RetrieveNodeOut(
                id=n.node_id, kind=n.kind, subkind=n.subkind, title=n.title,
                snippet=n.snippet, score=n.score, why=n.why,
            )
            for n in resp.nodes
        ],
        citations=[
            CitationOut(
                node_id=c.node_id, contributing_score=c.contributing_score,
                edges_traversed=c.edges_traversed,
            )
            for c in resp.citations
        ],
        trace_id=rid,
    )
