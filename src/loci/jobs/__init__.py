"""Background jobs (v2).

The queue is SQLite-backed. The worker is a daemon thread spun up inside the
FastAPI process at startup. Atomic claim uses UPDATE...RETURNING.

Job kinds:
    classify_aspects — tag a newly-ingested resource with aspect vocab
    parse_links      — extract wikilinks + citations, write concept_edges
    log_usage        — flush a usage event to resource_usage_log
    embed_missing    — re-embed raw nodes that have no node_vec entry

Subpackages:
    queue   — enqueue / claim / progress / mark
    worker  — polling loop + handler dispatch
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
