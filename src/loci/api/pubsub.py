"""In-process publish/subscribe for WebSocket fan-out.

Two channels:

- `project:{id}` — graph deltas (node created, edge created, status changed)
  and trace events (CitationTracker writes).
- `job:{id}` — progress updates from background workers.

Subscribers are asyncio queues; publishers post events that get fanned out.
The implementation is intentionally tiny — no external broker — because loci
is a single-process local server. If we ever shard, this gets replaced by
Redis pub/sub or NATS, but the shape stays the same.

Two publish flavours are exposed because the loci server has both async route
handlers (WebSocket endpoints) and sync route handlers (most REST routes,
written `def` rather than `async def` so they execute on FastAPI's threadpool).

- `bus.publish(channel, event)` — coroutine, callable from async code.
- `bus.publish_sync(channel, event)` — schedules the publish on the running
  asyncio loop. Safe to call from sync route handlers and worker threads.
  If no loop is running (e.g. unit tests that drive the bus directly), the
  call falls through to a synchronous queue put_nowait. Slow consumers (full
  queue) are dropped — we'd rather lose a delta than block the publisher.

Each channel also carries a monotonic `seq` counter (see `next_seq`) so the
frontend can detect missed events on reconnect and request a backfill from
`seq+1`. The counter is in-process state — survive process restart isn't
required (the frontend treats `seq=0` on reconnect as "I just connected").
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import defaultdict
from typing import Any

log = logging.getLogger(__name__)


class PubSub:
    """One bus per process. Channels are arbitrary string keys."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = defaultdict(list)
        self._lock = asyncio.Lock()
        # Per-project monotonic counter. Keyed by project_id, not channel,
        # because the WS hello and the trace events both want the same number.
        self._seq: dict[str, int] = defaultdict(int)
        self._seq_lock = threading.Lock()
        # The asyncio loop that owns `_subscribers`. Set by `attach_loop()`
        # at FastAPI startup so sync threads can schedule publishes via
        # `run_coroutine_threadsafe`. Tests can leave it as None and call
        # `_publish_now` directly.
        self._loop: asyncio.AbstractEventLoop | None = None

    # -----------------------------------------------------------------------
    # Loop attachment (used by sync→async bridge)
    # -----------------------------------------------------------------------

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the asyncio loop so `publish_sync` can schedule onto it."""
        self._loop = loop

    # -----------------------------------------------------------------------
    # Sequence counter
    # -----------------------------------------------------------------------

    def next_seq(self, project_id: str) -> int:
        """Allocate the next monotonic sequence number for `project_id`.

        Called once per published event. Thread-safe under a regular lock —
        we don't want async overhead on the publish path.
        """
        with self._seq_lock:
            self._seq[project_id] += 1
            return self._seq[project_id]

    def current_seq(self, project_id: str) -> int:
        """Return the latest issued seq, or 0 if nothing has been published yet."""
        with self._seq_lock:
            return self._seq.get(project_id, 0)

    # -----------------------------------------------------------------------
    # Subscribe / publish
    # -----------------------------------------------------------------------

    async def subscribe(self, channel: str) -> asyncio.Queue[dict[str, Any]]:
        """Return a Queue that will receive events for `channel`. Caller is
        responsible for `unsubscribe(channel, queue)` on disconnect."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        async with self._lock:
            self._subscribers[channel].append(q)
        return q

    async def unsubscribe(self, channel: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        import contextlib
        async with self._lock:
            with contextlib.suppress(ValueError):
                self._subscribers[channel].remove(queue)

    async def publish(self, channel: str, event: dict[str, Any]) -> None:
        """Fan-out to all subscribers. Slow consumers (full queue) get dropped
        for this event — we'd rather lose a delta than block the publisher."""
        async with self._lock:
            queues = list(self._subscribers.get(channel, []))
        self._fanout(channel, event, queues)

    def publish_sync(self, channel: str, event: dict[str, Any]) -> None:
        """Publish from sync code (FastAPI sync routes, worker threads).

        If a running loop is attached, schedule `publish` on it via
        `run_coroutine_threadsafe`. Otherwise (tests that drive the bus
        without an event loop) do the fanout inline — the queues are still
        asyncio.Queue but their `put_nowait` is thread-safe enough for
        single-process tests that immediately await `q.get()`.
        """
        loop = self._loop
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(self.publish(channel, event), loop)
            return
        # Fallback: synchronous fanout. Snapshot subscribers without the
        # async lock — acceptable in tests; the lock isn't held outside async.
        queues = list(self._subscribers.get(channel, []))
        self._fanout(channel, event, queues)

    def _fanout(
        self,
        channel: str,
        event: dict[str, Any],
        queues: list[asyncio.Queue[dict[str, Any]]],
    ) -> None:
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("pubsub: subscriber on %s is full; dropping event", channel)


# Process-global bus. Tests can replace via `loci.api.pubsub.bus = PubSub()`.
bus = PubSub()
