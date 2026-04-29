"""SQLite connection helper.

Why a wrapper exists at all:

1. We need to load the `sqlite-vec` loadable extension on every fresh
   connection — the Python `sqlite3.Connection` doesn't know about it.
2. We want sane defaults for a write-mostly local server: WAL journal,
   FOREIGN KEYS on, NORMAL synchronous (WAL is durable enough for our
   single-user use), busy_timeout long enough that schema-migration locks
   don't surprise concurrent readers.
3. Connections are per-thread for Python's `sqlite3` module; FastAPI/uvicorn
   may use a thread pool for sync routes plus the event loop for async ones.
   We expose `connect()` (always-fresh, caller manages lifetime) and
   `get_connection()` (thread-local cached) for the two patterns.

The schema is owned by `loci.db.schema`; this module is purely about getting
a usable connection.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec

from loci.config import Settings, get_settings

# One connection per thread. We keep a thread-local cache as a perf optimization
# (background workers reuse their handle), but we also pass check_same_thread=False
# so a connection can cross threads within a single FastAPI request — sub-dependencies
# and the endpoint each run in separate threadpool workers, and FastAPI caches the
# Depends() result, so the connection created in worker A may be used in worker B.
# Concurrency within a request is serialized by `await`; across requests, SQLite's
# own busy_timeout + WAL journal handle contention.
_local = threading.local()


def connect(db_path: Path | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a fresh SQLite connection with sqlite-vec attached.

    Returns a brand-new connection on every call — the caller owns it. For the
    request-scoped path, see `get_connection()`.

    `read_only=True` opens in `mode=ro` via URI. The sqlite-vec extension is
    still loaded (it's load-time only; queries are normal SQL).
    """
    settings = get_settings()
    path = db_path or settings.db_path
    settings.ensure_dirs()

    if read_only:
        # URI form is the only way to get mode=ro on a stock connection.
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(
            uri, uri=True, isolation_level=None, timeout=30.0, check_same_thread=False,
        )
    else:
        # isolation_level=None gives us autocommit; we do explicit BEGIN/COMMIT
        # in repositories where transactions matter. This avoids surprise with
        # implicit BEGIN's that block other writers.
        conn = sqlite3.connect(
            path, isolation_level=None, timeout=30.0, check_same_thread=False,
        )

    _configure(conn, read_only=read_only)
    _attach_vec(conn)
    return conn


def _configure(conn: sqlite3.Connection, *, read_only: bool) -> None:
    """Apply the loci pragma profile.

    PRAGMA reasoning:
    - `journal_mode=WAL`: required for concurrent readers + a writer. Skipped
      for read-only connections (PRAGMA fails silently anyway).
    - `synchronous=NORMAL`: WAL + NORMAL is safe against power loss for app
      crashes; only OS crash can lose the last txn. Acceptable for personal use.
    - `foreign_keys=ON`: FK constraints in the schema are real. Default-off in
      SQLite is one of its most-asked-about footguns.
    - `temp_store=MEMORY`: spilled temp tables (large sorts/joins during the
      retrieve fan-out) stay in RAM rather than the system tempdir.
    - `mmap_size=256 MB`: the OS page cache backs SQLite reads anyway, but mmap
      cuts a syscall per page on the fast path. 256 MB is a soft cap — SQLite
      will use less if the DB is smaller.
    """
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA mmap_size = 268435456")  # 256 MB
    if not read_only:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")


def _attach_vec(conn: sqlite3.Connection) -> None:
    """Attach the sqlite-vec loadable extension.

    sqlite-vec ships a platform-specific shared object inside the wheel and
    `sqlite_vec.load()` finds it. We have to flip `enable_load_extension` on
    around the call — Python disables loadable extensions by default for
    safety.
    """
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        # Always turn it back off so application SQL can't load arbitrary .so
        # files via `select load_extension(...)`.
        conn.enable_load_extension(False)


def get_connection() -> sqlite3.Connection:
    """Return a thread-local connection, opening it on first call.

    Use this from API request handlers / job workers. The connection is closed
    when the thread exits (Python's threading shuts down `_local` cleanly).
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = connect()
        _local.conn = conn
    return conn


def close_thread_connection() -> None:
    """Close the thread-local connection if any. Idempotent."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


@contextmanager
def transaction(conn: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
    """Context manager wrapping a write transaction.

    Usage:
        with transaction() as tx:
            tx.execute("INSERT ...")
            tx.execute("UPDATE ...")
        # COMMIT happens on clean exit; ROLLBACK on exception.

    We use BEGIN IMMEDIATE so the writer lock is taken at BEGIN time rather
    than at first write, which avoids the "SQLITE_BUSY at COMMIT" surprise
    when two writers race.
    """
    target = conn or get_connection()
    target.execute("BEGIN IMMEDIATE")
    try:
        yield target
    except Exception:
        target.execute("ROLLBACK")
        raise
    else:
        target.execute("COMMIT")


__all__ = [
    "connect",
    "get_connection",
    "close_thread_connection",
    "transaction",
    "Settings",
]
