"""Response endpoint — citation expansion.

PLAN.md §API §Citation expansion:

    GET /responses/:id      full response + citations
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from loci.api.dependencies import db
from loci.citations import CitationTracker

router = APIRouter(prefix="/responses", tags=["responses"])


@router.get("/{response_id}")
def get_response(
    response_id: str, conn: sqlite3.Connection = Depends(db),
) -> dict:
    rec = CitationTracker(conn).get_response(response_id)
    if rec is None:
        raise HTTPException(404, detail="response not found")
    return rec
