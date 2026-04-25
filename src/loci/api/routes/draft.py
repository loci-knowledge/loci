"""Draft endpoint stub — implementation lives in `loci.draft`.

PLAN.md §API §Drafting is the operation Claude Code will hit most. The full
LLM orchestration is in Phase 8 (`loci.draft`). This route just adapts request
shape and forwards.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field

from loci.api.dependencies import db, project_by_id
from loci.graph.models import Project

router = APIRouter(prefix="/projects", tags=["draft"])


class DraftBody(BaseModel):
    instruction: str
    context_md: str | None = None
    anchors: list[str] = Field(default_factory=list)
    style: str = "prose"  # prose | outline | code-comments | bibtex
    cite_density: str = "normal"  # low | normal | high
    session_id: str = "default"
    hyde: bool = False
    k: int = 12


class DraftCitationOut(BaseModel):
    node_id: str
    kind: str
    subkind: str
    title: str
    why_cited: str
    raw_supports: list[str]


class DraftResponseBody(BaseModel):
    output_md: str
    citations: list[DraftCitationOut]
    response_id: str


@router.post("/{project_id}/draft")
def post_draft(
    body: DraftBody,
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
    user_agent: str = Header("unknown"),
) -> DraftResponseBody:
    # Imported lazily so a request hitting the route exercises the LLM stack
    # only when needed; tests of other routes don't pay for it.
    from loci.draft import DraftRequest, draft

    req = DraftRequest(
        project_id=project.id,
        session_id=body.session_id,
        instruction=body.instruction,
        context_md=body.context_md,
        anchors=body.anchors,
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
            DraftCitationOut(**c.__dict__) for c in result.citations
        ],
        response_id=result.response_id,
    )
