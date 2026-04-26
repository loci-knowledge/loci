"""Draft endpoint — raws-only citations + locus-routed trace table.

Response shape:
    {
      output_md:     "...",
      citations:     [{ node_id, kind=raw, subkind, title, why_cited, routed_by[] }, ...],
      routing_loci:  [{ id, subkind, title, relation_md, overlap_md,
                        source_anchor_md, angle, score }, ...],
      trace_table:   [{ raw_id, raw_title, interp_path: [{id, edge, to}, ...] }, ...],
      response_id:   str,
    }
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

from loci.api.dependencies import db, project_by_id
from loci.graph.models import Project

router = APIRouter(prefix="/projects", tags=["draft"])


class DraftBody(BaseModel):
    instruction: str
    context_md: str | None = None
    anchors: list[str] | None = None
    style: str = "prose"
    cite_density: str = "normal"
    session_id: str = "default"
    hyde: bool = False
    k: int = 12


class DraftCitationOut(BaseModel):
    node_id: str
    kind: str
    subkind: str
    title: str
    why_cited: str
    routed_by: list[str]


class RoutingLocusOut(BaseModel):
    id: str
    subkind: str
    title: str
    relation_md: str
    overlap_md: str
    source_anchor_md: str
    angle: str | None
    score: float


class DraftResponseBody(BaseModel):
    output_md: str
    citations: list[DraftCitationOut]
    routing_loci: list[RoutingLocusOut]
    trace_table: list[dict]
    response_id: str


@router.post("/{project_id}/draft")
def post_draft(
    body: DraftBody,
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
    user_agent: str = Header("unknown"),
) -> DraftResponseBody:
    from loci.api.routes.anchors import get_active_anchors
    from loci.draft import DraftRequest, draft

    anchors = list(body.anchors) if body.anchors is not None else get_active_anchors(project.id)

    req = DraftRequest(
        project_id=project.id,
        session_id=body.session_id,
        instruction=body.instruction,
        context_md=body.context_md,
        anchors=anchors,
        style=body.style,  # type: ignore[arg-type]
        cite_density=body.cite_density,  # type: ignore[arg-type]
        hyde=body.hyde,
        k=body.k,
        client=user_agent,
    )
    result = draft(conn, req)
    return DraftResponseBody(
        output_md=result.output_md,
        citations=[
            DraftCitationOut(
                node_id=c.node_id, kind=c.kind, subkind=c.subkind,
                title=c.title, why_cited=c.why_cited, routed_by=c.routed_by,
            )
            for c in result.citations
        ],
        routing_loci=[
            RoutingLocusOut(
                id=rl.node_id, subkind=rl.subkind, title=rl.title,
                relation_md=rl.relation_md, overlap_md=rl.overlap_md,
                source_anchor_md=rl.source_anchor_md, angle=rl.angle,
                score=rl.score,
            )
            for rl in result.routing_loci
        ],
        trace_table=result.trace_table,
        response_id=result.response_id,
    )
