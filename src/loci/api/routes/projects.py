"""Project endpoints.

Routes:
    GET  /projects                       list (most-recently-active first)
    POST /projects
    GET  /projects/:id
    PATCH /projects/:id/profile
    GET  /projects/:id/pinned            pinned node ids
    GET  /projects/:id/communities       latest community snapshot
"""

from __future__ import annotations

import calendar
import json
import sqlite3
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from loci.api.dependencies import db, project_by_id
from loci.graph.models import Project
from loci.graph.projects import ProjectRepository

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



# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class ProjectListItem(BaseModel):
    id: str
    slug: str
    name: str
    created_at: str
    last_active_at: str | None


class ProjectListResponse(BaseModel):
    projects: list[ProjectListItem]


@router.get("")
def list_projects(conn: sqlite3.Connection = Depends(db)) -> ProjectListResponse:
    """List all projects, most-recently-active first.

    The frontend's project picker hits this on activation. Ordering matches
    `last_active_at DESC NULLS LAST, created_at DESC` so a freshly-created
    project still surfaces near the top while it has no activity yet.

    Example response:
        {"projects": [
            {"id": "01ABC...", "slug": "loci",
             "name": "Loci", "created_at": "2026-04-20T...",
             "last_active_at": "2026-04-24T..."}
        ]}
    """
    rows = conn.execute(
        """
        SELECT id, slug, name, created_at, last_active_at
        FROM projects
        ORDER BY
            CASE WHEN last_active_at IS NULL THEN 1 ELSE 0 END,
            last_active_at DESC,
            created_at DESC
        """,
    ).fetchall()
    return ProjectListResponse(
        projects=[
            ProjectListItem(
                id=r["id"], slug=r["slug"], name=r["name"],
                created_at=r["created_at"],
                last_active_at=r["last_active_at"],
            )
            for r in rows
        ],
    )


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


# ---------------------------------------------------------------------------
# Pinned nodes (for the frontend's stability hint)
# ---------------------------------------------------------------------------


class PinnedResponse(BaseModel):
    pinned_node_ids: list[str]


@router.get("/{project_id}/pinned")
def get_pinned(
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> PinnedResponse:
    """Return the ids of nodes pinned within this project.

    Backed by `project_membership` rows where `role = 'pinned'`. The frontend
    uses this to keep pinned nodes spatially stable across renders.

    Example response:
        {"pinned_node_ids": ["01ABC...", "01DEF..."]}
    """
    rows = conn.execute(
        """
        SELECT node_id FROM project_membership
        WHERE project_id = ? AND role = 'pinned'
        ORDER BY added_at
        """,
        (project.id,),
    ).fetchall()
    return PinnedResponse(pinned_node_ids=[r["node_id"] for r in rows])


# ---------------------------------------------------------------------------
# Communities (latest snapshot)
# ---------------------------------------------------------------------------


class CommunityItem(BaseModel):
    id: str
    label: str | None
    member_node_ids: list[str]
    snapshot_at: str
    level: int


class CommunitiesResponse(BaseModel):
    communities: list[CommunityItem]
    community_version: int


def _snapshot_at_to_version(snapshot_at: str | None) -> int:
    """Translate the latest snapshot ISO timestamp to a monotonic int.

    We use epoch seconds; truncating sub-second resolution is fine because
    the absorb job re-snapshots at most once per pass and pass cadence is
    on the order of minutes. Returns 0 when there's no snapshot yet.
    """
    if not snapshot_at:
        return 0
    # Accept '...Z' (UTC) and bare ISO8601. fromisoformat in py3.11+ handles
    # 'YYYY-MM-DDTHH:MM:SS.fffZ' once we strip the 'Z'.
    s = snapshot_at[:-1] if snapshot_at.endswith("Z") else snapshot_at
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return 0
    return calendar.timegm(dt.timetuple())


def _latest_communities(
    conn: sqlite3.Connection, project_id: str,
) -> tuple[list[dict], str | None]:
    """Return (rows, latest_snapshot_at) for the most recent snapshot."""
    latest_row = conn.execute(
        "SELECT MAX(snapshot_at) AS latest FROM communities WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    latest = latest_row["latest"] if latest_row else None
    if not latest:
        return [], None
    rows = conn.execute(
        """
        SELECT id, label, member_node_ids, snapshot_at, level
        FROM communities
        WHERE project_id = ? AND snapshot_at = ?
        ORDER BY level, id
        """,
        (project_id, latest),
    ).fetchall()
    return [dict(r) for r in rows], latest


@router.get("/{project_id}/communities")
def get_communities(
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> CommunitiesResponse:
    """Return the latest community snapshot for a project.

    The absorb job runs Leiden community detection (when `loci[graph]` is
    installed) and writes one row per community per snapshot to the
    `communities` table. We surface only the most recent snapshot — older
    ones are kept for diffing but aren't useful to the frontend's districting.
    `community_version` is the snapshot timestamp in epoch seconds and is
    monotonic enough for the frontend's "should I re-district?" check. If
    no snapshot exists the response is `{"communities": [], "community_version": 0}`.

    Example response:
        {
          "communities": [
            {"id": "01...", "label": null, "member_node_ids": ["01A...", "01B..."],
             "snapshot_at": "2026-04-24T10:00:00.000Z", "level": 0}
          ],
          "community_version": 1745496000
        }
    """
    rows, latest = _latest_communities(conn, project.id)
    return CommunitiesResponse(
        communities=[
            CommunityItem(
                id=r["id"], label=r["label"],
                member_node_ids=json.loads(r["member_node_ids"]),
                snapshot_at=r["snapshot_at"], level=r["level"],
            )
            for r in rows
        ],
        community_version=_snapshot_at_to_version(latest),
    )


@router.patch("/{project_id}/profile")
def update_profile(
    body: UpdateProfile,
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    ProjectRepository(conn).update_profile(project.id, body.profile_md)
    return {"updated": True}


