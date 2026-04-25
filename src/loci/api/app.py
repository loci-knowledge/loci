"""FastAPI app factory.

`create_app()` is the entry point — used by `uvicorn.run("loci.api.app:create_app",
factory=True)` and by the CLI's `loci server` command. Tests construct a fresh
app per test by calling `create_app()` directly.

App lifespan: at startup we run migrations (idempotent) so a fresh data dir
just works. Shutdown is a no-op — connections are closed by Python's atexit.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from loci import __version__
from loci.config import get_settings
from loci.db import migrate

log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Run migrations + warm caches before serving traffic."""
    settings = get_settings()
    settings.ensure_dirs()
    applied = migrate()
    if applied:
        log.info("Applied migrations: %s", applied)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="loci",
        version=__version__,
        description=(
            "Personal memory graph server. Three layers (raw / interpretation / "
            "project), one citation contract."
        ),
        lifespan=_lifespan,
    )

    # Routers are imported here (not at module top) so app construction is
    # cheap and tests can swap implementations.
    from loci.api.routes import (
        draft,
        edges,
        feedback,
        graph_view,
        jobs,
        nodes,
        projects,
        proposals,
        responses,
        retrieve,
        sources,
    )

    app.include_router(projects.router)
    app.include_router(sources.router)
    app.include_router(retrieve.router)
    app.include_router(draft.router)
    app.include_router(feedback.router)
    app.include_router(nodes.router)
    app.include_router(edges.router)
    app.include_router(proposals.router)
    app.include_router(graph_view.router)
    app.include_router(jobs.router)
    app.include_router(responses.router)

    # WebSocket routes are registered directly on the app.
    from loci.api.websocket import register_ws

    register_ws(app)

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "version": __version__}

    return app
