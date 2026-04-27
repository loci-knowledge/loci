"""Serve the hosted graph editor SPA.

    GET /graph/:project_id   → index.html with PROJECT_ID substituted
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["graph-ui"])

_STATIC_DIR = Path(__file__).parent.parent / "static" / "graph_ui"


@router.get("/graph/{project_id}", response_class=HTMLResponse)
def graph_ui(project_id: str) -> HTMLResponse:
    """Return the graph editor SPA with the project_id baked in."""
    html = (_STATIC_DIR / "index.html").read_text()
    html = html.replace("__PROJECT_ID__", project_id)
    return HTMLResponse(content=html)
