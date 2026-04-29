"""One-shot schema initializer.

The schema is a single canonical file (`schema.sql`). On every connect we run
it through `executescript` — every statement is `CREATE … IF NOT EXISTS` so the
call is idempotent and there is no migration history to track. When the schema
changes we rewrite `schema.sql` and the database is reset (`loci reset`).
"""

from __future__ import annotations

import logging
from importlib import resources
from pathlib import Path

from loci.db.connection import connect

log = logging.getLogger(__name__)

_SCHEMA_PACKAGE = "loci.db"
_SCHEMA_FILE = "schema.sql"


def _read_schema() -> str:
    return resources.files(_SCHEMA_PACKAGE).joinpath(_SCHEMA_FILE).read_text(encoding="utf-8")


def init_schema(db_path: Path | None = None) -> None:
    """Apply the canonical schema. Idempotent."""
    sql = _read_schema()
    conn = connect(db_path)
    try:
        conn.executescript(sql)
    finally:
        conn.close()


__all__ = ["init_schema"]
