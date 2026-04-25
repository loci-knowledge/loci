"""Active-anchor endpoints (frontend "Pin for Claude Code").

These endpoints maintain a per-project transient set of node ids — the
"active anchors" — that retrieve and draft consult as the default seed when
the request's own `anchors` field is missing.

    POST  /projects/{project_id}/anchors  body: {node_ids: [...], ttl_sec: int}
    GET   /projects/{project_id}/anchors  → {node_ids: [...], expires_at: iso}

Storage is an in-memory dict keyed by project_id. This is a single-process
local server; we don't persist anchors to SQLite because they're explicitly
short-lived (default ttl 10 minutes) and a process restart is a perfectly
fine reason to clear them. Survive-process-restart isn't required.

Read paths (`retrieve.py`, `draft.py`) call `get_active_anchors(project_id)`
on this module to fetch the current set with expiry handling.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from loci.api.dependencies import project_by_id
from loci.graph.models import Project

router = APIRouter(prefix="/projects", tags=["anchors"])


# ---------------------------------------------------------------------------
# In-process store
# ---------------------------------------------------------------------------


class _Entry:
    __slots__ = ("node_ids", "expires_at_epoch")

    def __init__(self, node_ids: list[str], expires_at_epoch: float) -> None:
        self.node_ids = node_ids
        self.expires_at_epoch = expires_at_epoch


_store: dict[str, _Entry] = {}
_lock = threading.Lock()


def _now_epoch() -> float:
    return time.time()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{int((epoch % 1) * 1000):03d}Z"
    )


def get_active_anchors(project_id: str) -> list[str]:
    """Return the current active anchors, or [] if unset/expired.

    Pure read used by retrieve / draft fallbacks. Does not raise.
    """
    now = _now_epoch()
    with _lock:
        entry = _store.get(project_id)
        if entry is None:
            return []
        if entry.expires_at_epoch <= now:
            # Lazy expiry — drop the entry.
            del _store[project_id]
            return []
        return list(entry.node_ids)


def set_active_anchors(project_id: str, node_ids: list[str], ttl_sec: int) -> float:
    """Replace the active anchor set for a project. Returns the expiry epoch."""
    expires_at = _now_epoch() + max(0, int(ttl_sec))
    with _lock:
        _store[project_id] = _Entry(list(node_ids), expires_at)
    return expires_at


def clear_active_anchors(project_id: str) -> None:
    """Drop the active anchor set for a project. Used by tests."""
    with _lock:
        _store.pop(project_id, None)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AnchorsBody(BaseModel):
    node_ids: list[str] = Field(default_factory=list)
    ttl_sec: int = Field(default=600, ge=1, le=24 * 3600)


class AnchorsResponse(BaseModel):
    node_ids: list[str]
    expires_at: str | None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/{project_id}/anchors")
def post_anchors(
    body: AnchorsBody,
    project: Project = Depends(project_by_id),
) -> AnchorsResponse:
    """Set the project's active anchor set.

    The frontend's "Pin for Claude Code" feature posts here with a TTL
    (default 600s = 10 minutes). Subsequent retrieve / draft calls on the
    same project will fall back to this set when their own `anchors` field
    is omitted (None). An empty `node_ids` clears the set effectively (it
    persists but contributes nothing to the fallback).

    Example response:
        {"node_ids": ["01ABC...", "01DEF..."],
         "expires_at": "2026-04-24T10:00:00.000Z"}
    """
    if any(len(nid) != 26 for nid in body.node_ids):
        raise HTTPException(400, detail="node_ids must be 26-char ULIDs")
    expires_at = set_active_anchors(project.id, body.node_ids, body.ttl_sec)
    return AnchorsResponse(node_ids=list(body.node_ids), expires_at=_iso(expires_at))


@router.get("/{project_id}/anchors")
def get_anchors(
    project: Project = Depends(project_by_id),
) -> AnchorsResponse:
    """Return the current anchor set for a project.

    Returns `{"node_ids": [], "expires_at": null}` if unset or expired.

    Example response:
        {"node_ids": ["01ABC..."], "expires_at": "2026-04-24T10:00:00.000Z"}
    """
    now = _now_epoch()
    with _lock:
        entry = _store.get(project.id)
        if entry is None or entry.expires_at_epoch <= now:
            if entry is not None:
                del _store[project.id]
            return AnchorsResponse(node_ids=[], expires_at=None)
        return AnchorsResponse(
            node_ids=list(entry.node_ids),
            expires_at=_iso(entry.expires_at_epoch),
        )
