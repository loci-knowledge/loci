"""Shared draft orchestration.

Callers resolve project_id, then pass it here. This module owns: DraftRequest
construction → draft() → pending effects. The draft() function internally handles
CitationTracker write and reflect enqueue, so this module is thinner than retrieve.
Broadcasting is left to the adapter.
"""

from __future__ import annotations

import sqlite3
from typing import Any


def run_draft(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    instruction: str,
    context_md: str | None = None,
    anchors: list[str] | None = None,
    style: str = "prose",
    cite_density: str = "normal",
    k: int = 12,
    hyde: bool = False,
    session_id: str = "default",
    client: str = "unknown",
) -> Any:  # loci.draft.DraftResult
    """Run draft and return the DraftResult (with pending_effects populated).

    Returns the raw DraftResult object; adapters format it for their surface.
    """
    from loci.draft import DraftRequest
    from loci.draft import draft as _draft

    req = DraftRequest(
        project_id=project_id,
        session_id=session_id,
        instruction=instruction,
        context_md=context_md,
        anchors=anchors or [],
        style=style,  # type: ignore[arg-type]
        cite_density=cite_density,  # type: ignore[arg-type]
        k=k,
        hyde=hyde,
        client=client,
    )
    return _draft(conn, req)
