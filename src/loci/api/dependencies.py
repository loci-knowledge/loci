"""FastAPI dependencies: connection injection, project lookup helpers."""

from __future__ import annotations

import sqlite3

from fastapi import Depends, HTTPException, Path, status

from loci.db.connection import get_connection
from loci.graph.models import Project
from loci.graph.projects import ProjectRepository


def db() -> sqlite3.Connection:
    """Per-request DB connection. Thread-local under the hood so this is cheap."""
    return get_connection()


def project_by_id(
    project_id: str = Path(..., min_length=26, max_length=26),
    conn: sqlite3.Connection = Depends(db),
) -> Project:
    """Resolve a project by ULID; raise 404 if missing."""
    proj = ProjectRepository(conn).get(project_id)
    if proj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    return proj
