"""In-process publish/subscribe for WebSocket fan-out.

Two channels:

- `project:{id}` — graph deltas (node created, edge created, status changed).
- `job:{id}` — progress updates from background workers.

Subscribers are asyncio queues; publishers post events that get fanned out.
The implementation is intentionally tiny — no external broker — because loci
is a single-process local server. If we ever shard, this gets replaced by
Redis pub/sub or NATS, but the shape stays the same.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

log = logging.getLogger(__name__)


class PubSub:
    """One bus per process. Channels are arbitrary string keys."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = defaultdict(list)
        self._lock = asyncio.Lock()

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
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("pubsub: subscriber on %s is full; dropping event", channel)


# Process-global bus. Tests can replace via `loci.api.pubsub.bus = PubSub()`.
bus = PubSub()
