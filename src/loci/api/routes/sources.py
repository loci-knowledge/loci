"""Raw source management endpoints.

Replaces the old interpretation-node router. These routes expose read and
delete operations over raw source files that have been ingested into the graph.

Routes:
    GET    /projects/{project_id}/sources                   list sources (filtered)
    GET    /projects/{project_id}/sources/{resource_id}     get one source
    DELETE /projects/{project_id}/sources/{resource_id}     delete a source
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, status

from loci.api.dependencies import db, project_by_id
from loci.graph.aspects import AspectRepository
from loci.graph.models import Project
from loci.graph.sources import SourceRepository

router = APIRouter(prefix="/projects/{project_id}/sources", tags=["sources"])


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
def list_sources(
    project: Project = Depends(project_by_id),
    folder: str | None = Query(default=None, description="Filter by folder label"),
    aspect: str | None = Query(default=None, description="Filter by aspect label"),
    limit: int = Query(default=50, ge=1, le=500),
    conn: sqlite3.Connection = Depends(db),
) -> list[dict]:
    """List raw sources for a project, optionally filtered.

    When `folder` is supplied, only sources whose `canonical_path` is under
    that folder prefix are returned. When `aspect` is supplied, only sources
    tagged with that aspect label are returned. Both filters can be combined.
    Results are ordered by `created_at` descending.

    Example response:
        [{"id": "01ABC...", "title": "Attention Is All You Need",
          "subkind": "pdf", "mime": "application/pdf",
          "canonical_path": "/Users/.../papers/attention.pdf",
          "size_bytes": 204800, "created_at": "2026-04-20T...",
          "tags": ["transformers"], "aspects": ["nlp", "attention"]}]
    """
    src_repo = SourceRepository(conn)
    aspect_repo = AspectRepository(conn)

    if aspect:
        # Filter to resource IDs that carry this aspect within the project.
        resource_ids = aspect_repo.resources_for_aspect(aspect, project_id=project.id, limit=limit)
        raws = src_repo.get_many(resource_ids)
    else:
        raws = src_repo.list_by_project(project.id)

    if folder:
        raws = [r for r in raws if r.canonical_path.startswith(folder)]

    # Trim to limit after all filters are applied
    raws = raws[:limit]

    result = []
    for r in raws:
        ras = aspect_repo.aspects_for(r.id)
        aspect_labels = []
        for ra in ras:
            a = aspect_repo.get_by_id(ra.aspect_id)
            if a:
                aspect_labels.append(a.label)
        result.append({
            "id": r.id,
            "title": r.title,
            "subkind": r.subkind,
            "mime": r.mime,
            "canonical_path": r.canonical_path,
            "size_bytes": r.size_bytes,
            "content_hash": r.content_hash,
            "status": r.status,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
            "tags": r.tags,
            "aspects": aspect_labels,
        })
    return result


@router.get("/{resource_id}")
def get_source(
    resource_id: str,
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    """Get a specific source with its aspects and provenance.

    Returns the full raw node record plus all aspect associations and their
    confidence scores. Raises 404 if the resource does not exist or does not
    belong to this project.

    Example response:
        {"id": "01ABC...", "title": "...", "subkind": "pdf", ...,
         "aspects": [{"label": "nlp", "confidence": 1.0, "source": "user"}],
         "tags": ["transformers"]}
    """
    src_repo = SourceRepository(conn)
    raw = src_repo.get(resource_id)
    if raw is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")

    # Verify membership in this project
    row = conn.execute(
        """
        SELECT 1 FROM project_effective_members
        WHERE project_id = ? AND node_id = ?
        """,
        (project.id, resource_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")

    aspect_repo = AspectRepository(conn)
    ras = aspect_repo.aspects_for(resource_id)
    aspects_out = []
    for ra in ras:
        a = aspect_repo.get_by_id(ra.aspect_id)
        aspects_out.append({
            "label": a.label if a else ra.aspect_id,
            "aspect_id": ra.aspect_id,
            "confidence": ra.confidence,
            "source": ra.source,
            "created_at": ra.created_at,
        })

    return {
        "id": raw.id,
        "title": raw.title,
        "body": raw.body,
        "subkind": raw.subkind,
        "mime": raw.mime,
        "canonical_path": raw.canonical_path,
        "size_bytes": raw.size_bytes,
        "content_hash": raw.content_hash,
        "source_of_truth": raw.source_of_truth,
        "status": raw.status,
        "confidence": raw.confidence,
        "access_count": raw.access_count,
        "created_at": raw.created_at,
        "updated_at": raw.updated_at,
        "last_accessed_at": raw.last_accessed_at,
        "tags": raw.tags,
        "aspects": aspects_out,
    }


@router.delete("/{resource_id}", status_code=status.HTTP_200_OK)
def delete_source(
    resource_id: str,
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    """Delete a source from the project.

    Hard-deletes the raw node, its embeddings, chunk vectors, aspect
    associations, and all project/workspace membership rows. This is
    irreversible. Raises 404 if the resource does not exist or does not
    belong to this project.

    Example response:
        {"deleted": true, "resource_id": "01ABC..."}
    """
    src_repo = SourceRepository(conn)
    raw = src_repo.get(resource_id)
    if raw is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")

    # Verify membership in this project before deleting
    row = conn.execute(
        """
        SELECT 1 FROM project_effective_members
        WHERE project_id = ? AND node_id = ?
        """,
        (project.id, resource_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")

    src_repo.delete(resource_id)
    conn.commit()
    return {"deleted": True, "resource_id": resource_id}
