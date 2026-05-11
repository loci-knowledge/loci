"""Schema initializer + incremental migrations.

The base schema lives in `schema.sql` (all statements are CREATE … IF NOT
EXISTS, so applying it is idempotent). After the base schema, `run_migrations`
applies any pending incremental changes to existing databases (ALTER TABLE,
table recreations, etc.).
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
    """Apply the canonical schema then run pending migrations. Idempotent.

    Order matters on existing databases: migrations add columns (e.g.
    aspect_vocab.project_id) BEFORE executescript tries to create indexes
    that reference those columns. On fresh installs migrations detect missing
    tables and skip, letting executescript create everything from scratch.
    """
    from loci.db.migrations import _ensure_migrations_table, run_migrations

    sql = _read_schema()
    conn = connect(db_path)
    try:
        # 1. Bootstrap migrations tracking table (IF NOT EXISTS — always safe).
        _ensure_migrations_table(conn)
        # 2. Apply incremental migrations so existing tables gain new columns
        #    before executescript references them in index definitions.
        run_migrations(conn)
        # 3. Apply full schema (all IF NOT EXISTS — idempotent).
        conn.executescript(sql)
    finally:
        conn.close()


__all__ = ["init_schema"]
