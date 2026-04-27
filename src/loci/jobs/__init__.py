"""Background jobs.

PLAN.md §Background jobs:

    POST /projects/:id/absorb        enqueue a checkpoint
    GET  /jobs/:id                   status

The queue is SQLite-backed (no Redis). The worker is a daemon thread spun up
inside the FastAPI process at startup. For multi-process deployments the
queue table supports atomic claim via UPDATE...RETURNING; we just don't run
multiple workers today.

Subpackages:
    queue        — enqueue / claim / progress / mark
    worker       — polling loop + handler dispatch
    absorb       — the checkpoint pipeline (PLAN §Cost model: per absorb)
    contradiction — 3-way classifier over (raw, top-k interps)
    proposals    — alias detection, broken-support tensions, forgetting
    audits       — orphan / broken-support / bloat / thinning
    communities  — Leiden community detection (optional, igraph-gated)
"""

from loci.jobs.queue import append_job_step, enqueue, get_job, mark_failed
from loci.jobs.worker import run_worker_loop, start_worker_thread

__all__ = [
    "append_job_step",
    "enqueue",
    "get_job",
    "mark_failed",
    "run_worker_loop",
    "start_worker_thread",
]
