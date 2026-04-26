"""Job worker.

A simple polling loop that claims jobs and dispatches by `kind`. Two ways to
run it:

- `run_worker_loop()` — synchronous, runs forever in the current thread.
  Used by `loci worker` CLI command.
- `start_worker_thread()` — spawns a daemon thread; used by the FastAPI app
  to colocate the worker with the HTTP server.

Handlers:
    absorb   → loci.jobs.absorb.run
    kickoff  → seed initial questions for a new project
    reembed  → re-embed all dirty interpretation nodes
    reindex  → rebuild FTS / vec indices
    export   → markdown export

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

# Type for a handler: (conn, project_id, payload) -> result_dict
HandlerFn = Callable[[sqlite3.Connection, str | None, dict], dict]


def _handlers() -> dict[str, HandlerFn]:
    """Resolve handlers lazily so importing the worker doesn't pull in absorb
    (which imports the embedder, which imports torch...)."""
    from loci.jobs.absorb import run as run_absorb
    from loci.jobs.autoresearch import run as run_autoresearch
    from loci.jobs.kickoff import run as run_kickoff
    from loci.jobs.reflect import run as run_reflect
    from loci.jobs.relevance import run as run_relevance
    from loci.jobs.sweep_orphans import run as run_sweep_orphans
    return {
        "absorb": run_absorb,
        "autoresearch": run_autoresearch,
        "kickoff": run_kickoff,
        "reflect": run_reflect,
        "relevance": run_relevance,
        "sweep_orphans": run_sweep_orphans,
    }


def run_once(conn: sqlite3.Connection) -> bool:
    """Try to claim and run one job. Returns True if a job ran, False if queue empty."""
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
        # Surface the job id to handlers that want to publish progress.
        # Handlers that ignore the key (most of them) are unaffected.
        payload = dict(job["payload"] or {})
        payload.setdefault("__job_id", job["id"])
        result = handler(conn, job["project_id"], payload)
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
