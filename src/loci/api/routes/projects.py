"""Project endpoints + ingest endpoints (sources scan).

PLAN.md §API §Ingest and project setup:

    POST /projects
    POST /projects/:id/sources
    POST /projects/:id/sources/scan
    GET  /projects/:id
    PATCH /projects/:id/profile
"""

from __future__ import annotations

import sqlite3
from pathlib import Path as PPath

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from loci.api.dependencies import db, project_by_id
from loci.graph.models import Project
from loci.graph.projects import ProjectRepository
from loci.ingest import scan_path
from loci.ingest.content_hash import hash_file
from loci.ingest.extractors import extract

router = APIRouter(prefix="/projects", tags=["projects"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CreateProject(BaseModel):
    slug: str
    name: str
    profile_md: str = ""
    config: dict = Field(default_factory=dict)


class UpdateProfile(BaseModel):
    profile_md: str


class RegisterSource(BaseModel):
    """Register a single path or URL for ingest. Returns content_hash on success."""
    path: str  # absolute path or file:// URL


class ScanRequest(BaseModel):
    root: str  # absolute path to walk


class ScanResponse(BaseModel):
    scanned: int
    new_raw: int
    deduped: int
    skipped: int
    members_added: int
    errors: list[str]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED)
def create_project(
    body: CreateProject, conn: sqlite3.Connection = Depends(db),
) -> Project:
    repo = ProjectRepository(conn)
    if repo.get_by_slug(body.slug) is not None:
        raise HTTPException(409, detail=f"slug already taken: {body.slug}")
    project = Project(
        slug=body.slug, name=body.name, profile_md=body.profile_md,
        config=body.config,
    )
    return repo.create(project)


@router.get("/{project_id}")
def get_project(project: Project = Depends(project_by_id)) -> Project:
    return project


@router.patch("/{project_id}/profile")
def update_profile(
    body: UpdateProfile,
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    ProjectRepository(conn).update_profile(project.id, body.profile_md)
    return {"updated": True}


@router.post("/{project_id}/sources", status_code=status.HTTP_201_CREATED)
def register_source(
    body: RegisterSource,
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    """Register a single file. Returns the content hash + node id.

    For directory walks, use /sources/scan instead — it's batched and idempotent.
    """
    p = PPath(body.path).expanduser().resolve()
    if not p.is_file():
        raise HTTPException(400, detail=f"not a file: {p}")
    full, trunc, _size = hash_file(p)
    extracted = extract(p)
    if extracted is None:
        raise HTTPException(415, detail=f"unsupported file type: {p.suffix}")
    res = scan_path(conn, project.id, p)
    return {
        "content_hash": trunc,
        "full_hash": full,
        "result": res.__dict__,
    }


@router.post("/{project_id}/sources/scan")
def scan_sources(
    body: ScanRequest,
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> ScanResponse:
    root = PPath(body.root).expanduser().resolve()
    if not root.exists():
        raise HTTPException(404, detail=f"root not found: {root}")
    res = scan_path(conn, project.id, root)
    return ScanResponse(**res.__dict__)
