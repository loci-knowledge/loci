"""Edge endpoints.

PLAN.md §API §Graph manipulation:

    POST /edges
    DELETE /edges/:id

After-DB-commit, every mutation publishes a graph-delta event onto every
project channel that contains BOTH endpoints. The frontend's `deltaReducer`
applies the same edge upsert/delete shape via its generic `op/entity/payload`
fallthrough.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from loci.api.dependencies import db
from loci.api.publishers import (
    projects_for_edge,
    publish_edge_delete,
    publish_edge_upsert,
)
from loci.graph import EdgeRepository, NodeRepository
from loci.graph.edges import EdgeError
from loci.graph.models import EdgeType

router = APIRouter(prefix="/edges", tags=["edges"])


class CreateEdge(BaseModel):
    src: str
    dst: str
    type: EdgeType
    weight: float = Field(1.0, ge=0.0, le=1.0)


@router.post("", status_code=201)
def create_edge(
    body: CreateEdge, conn: sqlite3.Connection = Depends(db),
) -> dict:
    nodes_repo = NodeRepository(conn)
    if nodes_repo.get(body.src) is None or nodes_repo.get(body.dst) is None:
        raise HTTPException(404, detail="src or dst not found")
    try:
        edge = EdgeRepository(conn).create(
            body.src, body.dst, body.type, weight=body.weight,
        )
    except EdgeError as exc:
        # Direction violation, self-loop, or DAG cycle.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    publish_edge_upsert(conn, edge)
    return {"edges": [edge.model_dump()]}


@router.delete("/{edge_id}")
def delete_edge(
    edge_id: str, conn: sqlite3.Connection = Depends(db),
) -> dict:
    repo = EdgeRepository(conn)
    existing = repo.get(edge_id)
    project_ids: list[str] = []
    src = dst = None
    if existing is not None:
        # Snapshot the projects that need the delete BEFORE we drop the row,
        # so the membership join can still find the edge endpoints.
        src, dst = existing.src, existing.dst
        project_ids = projects_for_edge(conn, src, dst)
    repo.delete(edge_id)
    if existing is not None:
        publish_edge_delete(conn, edge_id, src=src, dst=dst, project_ids=project_ids)
    return {"deleted": True}
