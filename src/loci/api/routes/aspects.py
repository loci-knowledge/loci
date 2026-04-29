"""Aspect vocabulary and resource-tagging endpoints.

Routes:
    GET    /projects/{project_id}/aspects                       list aspect vocab with usage counts
    POST   /projects/{project_id}/aspects                       create or get an aspect label
    GET    /projects/{project_id}/aspects/resources/{resource_id}   list aspects for a resource
    POST   /projects/{project_id}/aspects/resources/{resource_id}/tags   tag a resource
    DELETE /projects/{project_id}/aspects/resources/{resource_id}/tags   untag a resource
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from loci.api.dependencies import db, project_by_id
from loci.graph.aspects import AspectRepository
from loci.graph.models import Project

router = APIRouter(prefix="/projects/{project_id}/aspects", tags=["aspects"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CreateAspect(BaseModel):
    label: str
    description: str | None = None


class TagRequest(BaseModel):
    labels: list[str]
    source: str = "user"


class UntagRequest(BaseModel):
    labels: list[str]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
def list_aspects(
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> list[dict]:
    """List all aspect vocab labels with usage counts for this project.

    Returns aspects that have at least one associated resource in the
    project's effective membership, ordered by label. Each entry includes
    the label, description, id, and the count of tagged resources.

    Example response:
        [{"id": "01ABC...", "label": "machine-learning", "description": null,
          "user_defined": true, "auto_inferred": false, "count": 5}]
    """
    repo = AspectRepository(conn)
    top = repo.top_aspects(project.id, limit=200)
    # Build a lookup for full aspect details
    aspects = {a.label: a for a in repo.list_vocab(project_id=project.id)}
    result = []
    seen_labels = set()
    for label, count in top:
        seen_labels.add(label)
        a = aspects.get(label)
        result.append({
            "id": a.id if a else None,
            "label": label,
            "description": a.description if a else None,
            "user_defined": a.user_defined if a else False,
            "auto_inferred": a.auto_inferred if a else False,
            "last_used": a.last_used if a else None,
            "count": count,
        })
    # Include vocab aspects that have no tagged resources in this project yet
    for label, a in aspects.items():
        if label not in seen_labels:
            result.append({
                "id": a.id,
                "label": a.label,
                "description": a.description,
                "user_defined": a.user_defined,
                "auto_inferred": a.auto_inferred,
                "last_used": a.last_used,
                "count": 0,
            })
    return result


@router.post("", status_code=status.HTTP_201_CREATED)
def create_aspect(
    body: CreateAspect,
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    """Create or get an aspect label.

    Idempotent: if the label already exists the existing record is returned
    with a 201. If `description` is supplied and the aspect is new it is set;
    if the aspect already exists the description is updated only when the
    caller supplies one.

    Example response:
        {"id": "01ABC...", "label": "machine-learning", "description": "ML papers",
         "user_defined": true, "auto_inferred": false, "created": true}
    """
    repo = AspectRepository(conn)
    existing = repo.get_by_label(body.label)
    created = existing is None
    aspect = repo.ensure_aspect(body.label, source="user")
    if body.description is not None:
        repo.update_vocab(aspect.id, description=body.description)
        aspect = repo.get_by_id(aspect.id)  # refresh
    return {
        "id": aspect.id,
        "label": aspect.label,
        "description": aspect.description,
        "user_defined": aspect.user_defined,
        "auto_inferred": aspect.auto_inferred,
        "last_used": aspect.last_used,
        "created_at": aspect.created_at,
        "created": created,
    }


@router.get("/resources/{resource_id}")
def resource_aspects(
    resource_id: str,
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> list[dict]:
    """List aspects for a specific resource.

    Returns all aspect associations for the given resource, ordered by
    confidence descending. Does not filter by project membership — if the
    resource exists in the DB its aspects are always visible.

    Example response:
        [{"resource_id": "01...", "aspect_id": "01...", "label": "nlp",
          "confidence": 1.0, "source": "user", "created_at": "2026-..."}]
    """
    repo = AspectRepository(conn)
    ras = repo.aspects_for(resource_id)
    result = []
    for ra in ras:
        a = repo.get_by_id(ra.aspect_id)
        result.append({
            "resource_id": ra.resource_id,
            "aspect_id": ra.aspect_id,
            "label": a.label if a else ra.aspect_id,
            "confidence": ra.confidence,
            "source": ra.source,
            "created_at": ra.created_at,
        })
    return result


@router.post("/resources/{resource_id}/tags", status_code=status.HTTP_200_OK)
def tag_resource(
    resource_id: str,
    body: TagRequest,
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    """Add aspect tags to a resource.

    Idempotent: re-tagging an existing (resource, aspect) pair updates the
    source. Unknown labels are created in the vocab automatically. Commits
    immediately.

    Example response:
        {"tagged": ["machine-learning", "nlp"], "resource_id": "01..."}
    """
    if not body.labels:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="labels must be a non-empty list",
        )
    repo = AspectRepository(conn)
    repo.tag_resource(resource_id, body.labels, source=body.source)
    conn.commit()
    return {"tagged": body.labels, "resource_id": resource_id}


@router.delete("/resources/{resource_id}/tags", status_code=status.HTTP_200_OK)
def untag_resource(
    resource_id: str,
    body: UntagRequest,
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    """Remove aspect tags from a resource.

    Unknown labels are silently ignored (no error). Commits immediately.

    Example response:
        {"untagged": ["nlp"], "resource_id": "01..."}
    """
    if not body.labels:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="labels must be a non-empty list",
        )
    repo = AspectRepository(conn)
    repo.untag_resource(resource_id, body.labels)
    conn.commit()
    return {"untagged": body.labels, "resource_id": resource_id}
