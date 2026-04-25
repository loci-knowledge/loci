"""Source-root registration endpoints.

PLAN.md treats files as living anywhere on the user's filesystem (Zotero,
Obsidian, code, transcripts). These routes let the user *register* a root
once, then `loci scan <project>` (or `POST /projects/:id/sources/scan-all`)
walks every registered root.

    POST   /projects/:id/sources/roots          register a root
    GET    /projects/:id/sources/roots          list registered roots
    DELETE /projects/:id/sources/roots/:src_id  remove a registration
    POST   /projects/:id/sources/scan-all       walk every registered root

The single-file `POST /projects/:id/sources` and the directory-walk
`POST /projects/:id/sources/scan` (in `routes/projects.py`) remain as before.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from loci.api.dependencies import db, project_by_id
from loci.graph.models import Project
from loci.graph.sources import SourceRepository
from loci.ingest import scan_registered_sources

router = APIRouter(prefix="/projects", tags=["sources"])


class AddSource(BaseModel):
    root: str          # absolute or `~/...` path; resolved server-side
    label: str | None = None


class SourceOut(BaseModel):
    id: str
    root_path: str
    label: str | None
    added_at: str
    last_scanned_at: str | None


@router.post(
    "/{project_id}/sources/roots",
    status_code=status.HTTP_201_CREATED,
    response_model=SourceOut,
)
def add_source_root(
    body: AddSource,
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> SourceOut:
    p = Path(body.root).expanduser()
    if not p.exists():
        raise HTTPException(404, detail=f"path does not exist: {p}")
    src = SourceRepository(conn).add(project.id, p, label=body.label)
    return SourceOut(**src.__dict__)


@router.get("/{project_id}/sources/roots", response_model=list[SourceOut])
def list_source_roots(
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> list[SourceOut]:
    return [SourceOut(**s.__dict__) for s in SourceRepository(conn).list(project.id)]


@router.delete("/{project_id}/sources/roots/{src_id}")
def remove_source_root(
    src_id: str,
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    ok = SourceRepository(conn).remove(project.id, src_id)
    if not ok:
        raise HTTPException(404, detail="source not registered")
    return {"deleted": True}


@router.post("/{project_id}/sources/scan-all")
def scan_all_sources(
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    res = scan_registered_sources(conn, project.id)
    return res.__dict__
