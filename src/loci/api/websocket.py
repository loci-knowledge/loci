"""WebSocket routes.

PLAN.md §API §VSCode-flavored:

    WS /projects/:id/subscribe       graph deltas
    WS /jobs/:id/subscribe           job progress

Both push events from the in-process pubsub bus. Clients receive JSON frames
and disconnect at will; there's no client→server protocol beyond connect.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from loci.api.pubsub import bus

log = logging.getLogger(__name__)


def register_ws(app: FastAPI) -> None:
    @app.websocket("/projects/{project_id}/subscribe")
    async def project_subscribe(ws: WebSocket, project_id: str) -> None:
        await ws.accept()
        channel = f"project:{project_id}"
        q = await bus.subscribe(channel)
        try:
            # Send a hello so the client knows the connection is live. We
            # include the current `seq` so a reconnecting client knows which
            # range it has already seen — the next published event will carry
            # `seq + 1`. (Backfill from `seq+1` is a future endpoint; for now
            # the value is informational so the client can detect skips.)
            await ws.send_json({
                "type": "subscribed",
                "channel": channel,
                "seq": bus.current_seq(project_id),
            })
            while True:
                event = await q.get()
                await ws.send_json(event)
        except WebSocketDisconnect:
            pass
        finally:
            await bus.unsubscribe(channel, q)

    @app.websocket("/jobs/{job_id}/subscribe")
    async def job_subscribe(ws: WebSocket, job_id: str) -> None:
        await ws.accept()
        channel = f"job:{job_id}"
        q = await bus.subscribe(channel)
        try:
            await ws.send_json({"type": "subscribed", "channel": channel})
            while True:
                event = await q.get()
                await ws.send_json(event)
                # Auto-close on terminal status — clients shouldn't have to know
                # to disconnect themselves.
                if event.get("status") in {"done", "failed", "cancelled"}:
                    break
        except WebSocketDisconnect:
            pass
        finally:
            await bus.unsubscribe(channel, q)
