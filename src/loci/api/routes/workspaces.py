"""Workspace endpoints.

Information workspaces are named, typed bags of source roots that can be
linked to multiple projects. These routes expose the full workspace lifecycle:

    POST   /workspaces                           create
    GET    /workspaces                           list all
    GET    /workspaces/:ws_id                    get one
    PATCH  /workspaces/:ws_id                    update name/description
    POST   /workspaces/:ws_id/sources            add a source root
    GET    /workspaces/:ws_id/sources            list source roots
    DELETE /workspaces/:ws_id/sources/:src_id    remove a source root
    POST   /workspaces/:ws_id/scan               scan all sources in workspace

    POST   /projects/:project_id/workspaces/:ws_id       link workspace to project
    DELETE /projects/:project_id/workspaces/:ws_id       unlink workspace from project
    GET    /projects/:project_id/workspaces              list linked workspaces
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from loci.api.dependencies import db, project_by_id
from loci.graph.models import Project, Workspace, WorkspaceKind, WorkspaceRole
from loci.graph.workspaces import WorkspaceRepository
from loci.ingest.pipeline import scan_workspace

router = APIRouter(tags=["workspaces"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CreateWorkspace(BaseModel):
    slug: str
    name: str
    kind: WorkspaceKind = "mixed"
    description_md: str = ""


class UpdateWorkspace(BaseModel):
    name: str | None = None
    description_md: str | None = None


class WorkspaceOut(BaseModel):
    id: str
    slug: str
    name: str
    kind: str
    description_md: str
    created_at: str
    last_active_at: str | None
    last_scanned_at: str | None


class AddSourceIn(BaseModel):
    root: str
    label: str | None = None


class SourceOut(BaseModel):
    id: str
    workspace_id: str
    root_path: str
    label: str | None
    added_at: str
    last_scanned_at: str | None


class LinkWorkspaceIn(BaseModel):
    role: WorkspaceRole = "reference"
    weight: float = Field(default=1.0, ge=0.0, le=10.0)


class LinkedWorkspaceOut(BaseModel):
    workspace: WorkspaceOut
    role: str
    weight: float
    linked_at: str
    last_relevance_pass_at: str | None


# ---------------------------------------------------------------------------
# Workspace CRUD
# ---------------------------------------------------------------------------


@router.post("/workspaces", status_code=status.HTTP_201_CREATED, response_model=WorkspaceOut)
def create_workspace(
    body: CreateWorkspace,
    conn: sqlite3.Connection = Depends(db),
) -> WorkspaceOut:
    ws_repo = WorkspaceRepository(conn)
    if ws_repo.get_by_slug(body.slug) is not None:
        raise HTTPException(409, detail=f"workspace slug already exists: {body.slug}")
    ws = Workspace(slug=body.slug, name=body.name, kind=body.kind, description_md=body.description_md)
    ws_repo.create(ws)
    return _ws_out(ws)


@router.get("/workspaces", response_model=list[WorkspaceOut])
def list_workspaces(conn: sqlite3.Connection = Depends(db)) -> list[WorkspaceOut]:
    return [_ws_out(ws) for ws in WorkspaceRepository(conn).list()]


@router.get("/workspaces/{ws_id}", response_model=WorkspaceOut)
def get_workspace(ws_id: str, conn: sqlite3.Connection = Depends(db)) -> WorkspaceOut:
    ws = WorkspaceRepository(conn).get(ws_id)
    if ws is None:
        raise HTTPException(404, detail="workspace not found")
    return _ws_out(ws)


@router.patch("/workspaces/{ws_id}", response_model=WorkspaceOut)
def update_workspace(
    ws_id: str,
    body: UpdateWorkspace,
    conn: sqlite3.Connection = Depends(db),
) -> WorkspaceOut:
    ws_repo = WorkspaceRepository(conn)
    ws = ws_repo.get(ws_id)
    if ws is None:
        raise HTTPException(404, detail="workspace not found")
    updates: dict = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.description_md is not None:
        updates["description_md"] = body.description_md
    if updates:
        sets = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE information_workspaces SET {sets} WHERE id = ?",
            (*updates.values(), ws_id),
        )
    return _ws_out(ws_repo.get(ws_id) or ws)


# ---------------------------------------------------------------------------
# Source management
# ---------------------------------------------------------------------------


@router.post(
    "/workspaces/{ws_id}/sources",
    status_code=status.HTTP_201_CREATED,
    response_model=SourceOut,
)
def add_workspace_source(
    ws_id: str,
    body: AddSourceIn,
    conn: sqlite3.Connection = Depends(db),
) -> SourceOut:
    ws_repo = WorkspaceRepository(conn)
    if ws_repo.get(ws_id) is None:
        raise HTTPException(404, detail="workspace not found")
    p = Path(body.root).expanduser()
    if not p.exists():
        raise HTTPException(404, detail=f"path does not exist: {p}")
    src = ws_repo.add_source(ws_id, p, label=body.label)
    return _src_out(src)


@router.get("/workspaces/{ws_id}/sources", response_model=list[SourceOut])
def list_workspace_sources(
    ws_id: str, conn: sqlite3.Connection = Depends(db),
) -> list[SourceOut]:
    ws_repo = WorkspaceRepository(conn)
    if ws_repo.get(ws_id) is None:
        raise HTTPException(404, detail="workspace not found")
    return [_src_out(s) for s in ws_repo.list_sources(ws_id)]


@router.delete("/workspaces/{ws_id}/sources/{src_id}", status_code=status.HTTP_200_OK)
def remove_workspace_source(
    ws_id: str,
    src_id: str,
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    ws_repo = WorkspaceRepository(conn)
    if ws_repo.get(ws_id) is None:
        raise HTTPException(404, detail="workspace not found")
    deleted = conn.execute(
        "DELETE FROM workspace_sources WHERE id = ? AND workspace_id = ?",
        (src_id, ws_id),
    ).rowcount
    if not deleted:
        raise HTTPException(404, detail="source not found")
    return {"deleted": True}


@router.post("/workspaces/{ws_id}/scan")
def scan_workspace_sources(
    ws_id: str, conn: sqlite3.Connection = Depends(db),
) -> dict:
    ws_repo = WorkspaceRepository(conn)
    if ws_repo.get(ws_id) is None:
        raise HTTPException(404, detail="workspace not found")
    result = scan_workspace(conn, ws_id)
    return result.__dict__


# ---------------------------------------------------------------------------
# Project↔workspace links
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{project_id}/workspaces/{ws_id}",
    status_code=status.HTTP_201_CREATED,
)
def link_workspace_to_project(
    ws_id: str,
    body: LinkWorkspaceIn,
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    ws_repo = WorkspaceRepository(conn)
    if ws_repo.get(ws_id) is None:
        raise HTTPException(404, detail="workspace not found")
    ws_repo.link_project(project.id, ws_id, role=body.role, weight=body.weight)
    return {"linked": True, "project_id": project.id, "workspace_id": ws_id, "role": body.role}


@router.delete("/projects/{project_id}/workspaces/{ws_id}", status_code=status.HTTP_200_OK)
def unlink_workspace_from_project(
    ws_id: str,
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    ws_repo = WorkspaceRepository(conn)
    if ws_repo.get(ws_id) is None:
        raise HTTPException(404, detail="workspace not found")
    ws_repo.unlink_project(project.id, ws_id)
    return {"unlinked": True, "project_id": project.id, "workspace_id": ws_id}


@router.get("/projects/{project_id}/workspaces", response_model=list[LinkedWorkspaceOut])
def list_project_workspaces(
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> list[LinkedWorkspaceOut]:
    ws_repo = WorkspaceRepository(conn)
    return [
        LinkedWorkspaceOut(
            workspace=_ws_out(ws),
            role=link.role,
            weight=link.weight,
            linked_at=link.linked_at,
            last_relevance_pass_at=link.last_relevance_pass_at,
        )
        for ws, link in ws_repo.linked_workspaces(project.id)
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ws_out(ws) -> WorkspaceOut:
    return WorkspaceOut(
        id=ws.id,
        slug=ws.slug,
        name=ws.name,
        kind=ws.kind,
        description_md=ws.description_md,
        created_at=ws.created_at,
        last_active_at=ws.last_active_at,
        last_scanned_at=ws.last_scanned_at,
    )


def _src_out(src) -> SourceOut:
    return SourceOut(
        id=src.id,
        workspace_id=src.workspace_id,
        root_path=str(src.root_path),
        label=src.label,
        added_at=src.added_at,
        last_scanned_at=src.last_scanned_at,
    )
