"""Migration runner.

Migrations live under `loci/db/migrations/` as numbered files: `0001_*.sql`,
`0002_*.sql`, etc. Each is applied at most once. Order is the integer prefix.

Why hand-roll instead of pulling in alembic / yoyo:

- The schema is owned by this repo, not by an ORM. We're writing SQL by hand
  for sqlite-vec (vec0 virtual tables) and FTS5 — Alembic's autogen would just
  get in the way.
- A 60-line runner is easier to reason about than a multi-package toolchain
  for a single-user local server. If we ever grow to a hosted deployment, the
  marginal cost of switching is small because migrations are still SQL files.

A migration may contain multiple statements separated by semicolons. We use
`executescript()` which runs them as a single batch but commits each implicitly
— sqlite-vec's vec0 module has its own create-time setup that doesn't always
play well with explicit BEGIN, so this is the safe path.
"""

from __future__ import annotations

import logging
import sqlite3
from importlib import resources
from pathlib import Path

from loci.db.connection import connect

log = logging.getLogger(__name__)

# importlib.resources path to the migrations directory inside the installed
# package. We use a Traversable so this works from a wheel install too.
_MIGRATIONS_PACKAGE = "loci.db.migrations"


def _list_migrations() -> list[tuple[int, str, str]]:
    """Return [(number, name, sql)] sorted by number."""
    migrations: list[tuple[int, str, str]] = []
    for entry in resources.files(_MIGRATIONS_PACKAGE).iterdir():
        # `entry` is a Traversable. Skip the package __init__ and any non-.sql.
        name = entry.name
        if not name.endswith(".sql"):
            continue
        # Files are `NNNN_description.sql` — parse the leading integer.
        try:
            num_str, _ = name.split("_", 1)
            number = int(num_str)
        except (ValueError, IndexError):
            log.warning("Skipping migration with malformed name: %s", name)
            continue
        sql = entry.read_text(encoding="utf-8")
        migrations.append((number, name, sql))
    migrations.sort(key=lambda x: x[0])
    return migrations


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _migrations (
            number INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )


def _applied_numbers(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT number FROM _migrations").fetchall()
    return {row[0] for row in rows}


def migrate(db_path: Path | None = None) -> list[str]:
    """Apply all pending migrations. Returns the list of newly-applied names."""
    conn = connect(db_path)
    try:
        _ensure_migrations_table(conn)
        applied = _applied_numbers(conn)
        newly_applied: list[str] = []
        for number, name, sql in _list_migrations():
            if number in applied:
                continue
            log.info("Applying migration %s", name)
            # executescript implicitly commits any open txn before running and
            # again after; safe for our autocommit baseline.
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO _migrations (number, name) VALUES (?, ?)",
                (number, name),
            )
            newly_applied.append(name)
        return newly_applied
    finally:
        conn.close()


__all__ = ["migrate"]
