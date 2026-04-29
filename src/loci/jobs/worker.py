"""Job worker.

A simple polling loop that claims jobs and dispatches by `kind`. Two ways to
run it:

- `run_worker_loop()` — synchronous, runs forever in the current thread.
  Used by `loci worker` CLI command.
- `start_worker_thread()` — spawns a daemon thread; used by the FastAPI app
  to colocate the worker with the HTTP server.

Handlers (v2 job types only):
    classify_aspects → classify aspects for a newly-ingested resource
    parse_links      → extract wikilinks + citations, write concept_edges
    log_usage        → flush a usage event to resource_usage_log
    embed_missing    → re-embed raw nodes that have no node_vec entry

Worker uses its own SQLite connection so it doesn't fight the request handlers
for the thread-local connection.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import traceback
from collections.abc import Callable

from loci.db.connection import connect
from loci.jobs.queue import claim_one, mark_done, mark_failed

log = logging.getLogger(__name__)

# Type for a handler: (job, conn, settings) -> result_dict
HandlerFn = Callable[[dict, sqlite3.Connection, object], dict]


def _handlers() -> dict[str, HandlerFn]:
    """Resolve handlers lazily so importing the worker doesn't pull in the
    embedder (which imports torch) until a job that needs it is actually claimed."""
    from loci.jobs.classify_aspects import handle_classify_aspects
    from loci.jobs.embed_missing import handle_embed_missing
    from loci.jobs.log_usage import handle_log_usage
    from loci.jobs.parse_links import handle_parse_links
    return {
        "classify_aspects": handle_classify_aspects,
        "parse_links": handle_parse_links,
        "log_usage": handle_log_usage,
        "embed_missing": handle_embed_missing,
    }


def run_once(conn: sqlite3.Connection) -> bool:
    """Try to claim and run one job. Returns True if a job ran, False if queue empty."""
    from loci.config import get_settings

    job = claim_one(conn)
    if job is None:
        return False
    log.info("worker: claimed job %s kind=%s", job["id"], job["kind"])
    handlers = _handlers()
    handler = handlers.get(job["kind"])
    if handler is None:
        mark_failed(conn, job["id"], f"no handler for kind={job['kind']}")
        return True
    try:
        import asyncio
        import inspect
        settings = get_settings()
        raw = handler(job, conn, settings)
        # New v2 handlers are async; old-style sync handlers return directly.
        result = asyncio.run(raw) if inspect.isawaitable(raw) else raw
        mark_done(conn, job["id"], result=result)
        log.info("worker: job %s done", job["id"])
    except Exception as exc:  # noqa: BLE001 — never let a job crash the worker
        tb = traceback.format_exc()
        log.exception("worker: job %s failed", job["id"])
        mark_failed(conn, job["id"], f"{exc}\n{tb[-2000:]}")
    return True


def run_worker_loop(poll_interval: float = 1.0, *, stop_event: threading.Event | None = None) -> None:
    """Run jobs forever. Set `stop_event` to ask the loop to exit cleanly."""
    conn = connect()
    log.info("worker: started (poll_interval=%ss)", poll_interval)
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                log.info("worker: stop event set; exiting")
                return
            ran = run_once(conn)
            if not ran:
                time.sleep(poll_interval)
    finally:
        conn.close()


def start_worker_thread(poll_interval: float = 1.0) -> tuple[threading.Thread, threading.Event]:
    """Spawn a daemon thread running the worker loop. Returns (thread, stop_event)."""
    stop_event = threading.Event()
    t = threading.Thread(
        target=run_worker_loop,
        kwargs={"poll_interval": poll_interval, "stop_event": stop_event},
        name="loci-worker",
        daemon=True,
    )
    t.start()
    return t, stop_event
