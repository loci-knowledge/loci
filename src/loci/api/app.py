"""FastAPI app factory.

`create_app()` is the entry point — used by `uvicorn.run("loci.api.app:create_app",
factory=True)` and by the CLI's `loci server` command. Tests construct a fresh
app per test by calling `create_app()` directly.

App lifespan: at startup we apply the canonical schema (idempotent) so a fresh
data dir just works, and we hand the running asyncio loop to the pubsub bus so
sync route handlers can schedule WS publishes onto it. Shutdown is a no-op —
connections are closed by Python's atexit.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from loci import __version__
from loci.api.pubsub import bus
from loci.config import get_settings
from loci.db import init_schema

log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Apply the schema + warm caches before serving traffic."""
    settings = get_settings()
    settings.ensure_dirs()
    init_schema()
    bus.attach_loop(asyncio.get_running_loop())
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="loci",
        version=__version__,
        description=(
            "Personal memory graph server. Two layers (raw sources / projects), "
            "aspect-tagged retrieval."
        ),
        lifespan=_lifespan,
    )

    # Routers are imported here (not at module top) so app construction is
    # cheap and tests can swap implementations.
    from loci.api.routes import (
        aspects,
        jobs,
        projects,
        sources,
        workspaces,
    )

    app.include_router(projects.router)
    app.include_router(workspaces.router)
    app.include_router(jobs.router)
    app.include_router(aspects.router)
    app.include_router(sources.router)

    # WebSocket routes are registered directly on the app.
    from loci.api.websocket import register_ws

    register_ws(app)

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "version": __version__}

    return app
