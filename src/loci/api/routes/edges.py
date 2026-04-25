"""Edge endpoints.

PLAN.md §API §Graph manipulation:

    POST /edges
    DELETE /edges/:id
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from loci.api.dependencies import db
from loci.graph import EdgeRepository, NodeRepository
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
    edges = EdgeRepository(conn).create(body.src, body.dst, body.type, weight=body.weight)
    return {"edges": [e.model_dump() for e in edges]}


@router.delete("/{edge_id}")
def delete_edge(
    edge_id: str, conn: sqlite3.Connection = Depends(db),
) -> dict:
    EdgeRepository(conn).delete(edge_id)
    return {"deleted": True}
