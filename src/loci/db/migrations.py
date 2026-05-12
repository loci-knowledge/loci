"""Incremental schema migrations for loci.

`run_migrations()` is called by `init_schema()` after applying the base
schema. It tracks applied versions in `schema_migrations` and runs pending
migrations in order.

Each migration is a plain Python function that receives an open connection.
Migrations must be idempotent: safe to re-run on an already-migrated DB.
"""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)


def run_migrations(conn: sqlite3.Connection) -> None:
    """Apply all pending migrations in version order."""
    _ensure_migrations_table(conn)
    applied = _applied_versions(conn)

    for version, fn in _MIGRATIONS:
        if version not in applied:
            log.info("migrations: applying version %d (%s)", version, fn.__name__)
            fn(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) "
                "VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                (version,),
            )
            log.info("migrations: version %d applied", version)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Migration 1 — Aspects v2: project-scoped interpretation layer
# ---------------------------------------------------------------------------

def _m001_aspects_v2(conn: sqlite3.Connection) -> None:
    """Migrate to per-project aspect interpretation model.

    Changes:
    1. Fix concept_edges column name (metadata_json → metadata) if needed.
    2. Recreate aspect_vocab with optional project_id + composite UNIQUE.
    3. Recreate jobs with an expanded / open kind CHECK.
    4. Add project_id to concept_edges.
    5. Add project_id + query to resource_usage_log.
    """
    _fix_concept_edges_metadata_column(conn)
    _recreate_aspect_vocab(conn)
    _recreate_jobs_open_kind(conn)
    _add_concept_edges_project_id(conn)
    _add_usage_log_columns(conn)


def _fix_concept_edges_metadata_column(conn: sqlite3.Connection) -> None:
    """Rename concept_edges.metadata_json → metadata if the old name exists."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "concept_edges" not in tables:
        return  # fresh install — table created by executescript later
    cols = {row[1] for row in conn.execute("PRAGMA table_info(concept_edges)")}
    if "metadata_json" in cols and "metadata" not in cols:
        conn.execute("ALTER TABLE concept_edges RENAME COLUMN metadata_json TO metadata")
        log.info("migrations: renamed concept_edges.metadata_json → metadata")
    elif "metadata" in cols:
        log.debug("migrations: concept_edges.metadata already correct")
    else:
        log.warning("migrations: concept_edges has neither metadata nor metadata_json column")


def _recreate_aspect_vocab(conn: sqlite3.Connection) -> None:
    """Add project_id to aspect_vocab and switch from global UNIQUE(label)
    to composite UNIQUE(label, COALESCE(project_id, ''))."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "aspect_vocab" not in tables:
        return  # fresh install — table created by executescript later
    cols = {row[1] for row in conn.execute("PRAGMA table_info(aspect_vocab)")}
    if "project_id" in cols:
        log.debug("migrations: aspect_vocab.project_id already exists")
        return

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                CREATE TABLE aspect_vocab_new (
                    id                       TEXT PRIMARY KEY,
                    label                    TEXT NOT NULL,
                    project_id               TEXT,
                    description              TEXT,
                    conceptnet_relation_hint TEXT,
                    user_defined             INTEGER NOT NULL DEFAULT 1,
                    auto_inferred            INTEGER NOT NULL DEFAULT 0,
                    last_used                TEXT,
                    created_at               TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO aspect_vocab_new(
                    id, label, project_id, description, conceptnet_relation_hint,
                    user_defined, auto_inferred, last_used, created_at
                )
                SELECT id, label, NULL, description, conceptnet_relation_hint,
                       user_defined, auto_inferred, last_used, created_at
                FROM aspect_vocab
                """
            )
            conn.execute("DROP TABLE aspect_vocab")
            conn.execute("ALTER TABLE aspect_vocab_new RENAME TO aspect_vocab")
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_aspect_vocab_label_scope
                ON aspect_vocab(label, COALESCE(project_id, ''))
                """
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    log.info("migrations: aspect_vocab recreated with project_id column")


def _recreate_jobs_open_kind(conn: sqlite3.Connection) -> None:
    """Recreate jobs table without the restrictive kind CHECK constraint
    so new job kinds can be added without schema migrations."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "jobs" not in tables:
        return  # fresh install — table created by executescript later
    # Check if the table already lacks a kind CHECK by attempting a test insert.
    # Simpler: just check if our new kinds would be accepted.
    try:
        conn.execute("SAVEPOINT test_kind")
        conn.execute(
            "INSERT INTO jobs(id, kind, payload, status, progress, created_at) "
            "VALUES ('__test__', 'infer_interpretation', '{}', 'queued', 0, datetime('now'))"
        )
        conn.execute("ROLLBACK TO SAVEPOINT test_kind")
        conn.execute("RELEASE SAVEPOINT test_kind")
        log.debug("migrations: jobs.kind already accepts new kinds")
        return
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK TO SAVEPOINT test_kind")
        conn.execute("RELEASE SAVEPOINT test_kind")

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                CREATE TABLE jobs_new (
                    id          TEXT PRIMARY KEY,
                    kind        TEXT NOT NULL,
                    project_id  TEXT REFERENCES projects(id) ON DELETE CASCADE,
                    payload     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload)),
                    status      TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
                        'queued','running','done','failed','cancelled'
                    )),
                    progress    REAL NOT NULL DEFAULT 0.0
                                    CHECK (progress >= 0.0 AND progress <= 1.0),
                    error       TEXT,
                    result      TEXT CHECK (result IS NULL OR json_valid(result)),
                    fingerprint TEXT,
                    step_log    TEXT DEFAULT NULL,
                    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    started_at  TEXT,
                    finished_at TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO jobs_new SELECT
                    id, kind, project_id, payload, status, progress,
                    error, result, fingerprint, step_log, created_at, started_at, finished_at
                FROM jobs
                """
            )
            conn.execute("DROP TABLE jobs")
            conn.execute("ALTER TABLE jobs_new RENAME TO jobs")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(fingerprint) "
                "WHERE fingerprint IS NOT NULL"
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    log.info("migrations: jobs table recreated without kind CHECK constraint")


def _add_concept_edges_project_id(conn: sqlite3.Connection) -> None:
    """Add concept_edges.project_id (nullable FK to projects)."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "concept_edges" not in tables:
        return  # fresh install — table created by executescript later
    cols = {row[1] for row in conn.execute("PRAGMA table_info(concept_edges)")}
    if "project_id" in cols:
        log.debug("migrations: concept_edges.project_id already exists")
        return
    conn.execute("ALTER TABLE concept_edges ADD COLUMN project_id TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_concept_edges_project ON concept_edges(project_id)"
    )
    # Composite unique index for idempotent add_edge with project scope.
    # COALESCE so (src, dst, type, NULL) == (src, dst, type, NULL) in the index.
    try:
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_concept_edges_triple
            ON concept_edges(src_id, dst_id, edge_type, COALESCE(project_id, ''))
            """
        )
    except sqlite3.OperationalError:
        # May fail if existing duplicate rows violate the new unique constraint.
        log.warning(
            "migrations: could not create idx_concept_edges_triple unique index "
            "(duplicate edges exist); skipping"
        )
    log.info("migrations: concept_edges.project_id added")


def _add_usage_log_columns(conn: sqlite3.Connection) -> None:
    """Add project_id and query columns to resource_usage_log."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "resource_usage_log" not in tables:
        return  # fresh install — table created by executescript later
    cols = {row[1] for row in conn.execute("PRAGMA table_info(resource_usage_log)")}
    if "project_id" not in cols:
        conn.execute("ALTER TABLE resource_usage_log ADD COLUMN project_id TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_log_project "
            "ON resource_usage_log(project_id, used_at)"
        )
        log.info("migrations: resource_usage_log.project_id added")
    if "query" not in cols:
        conn.execute("ALTER TABLE resource_usage_log ADD COLUMN query TEXT")
        log.info("migrations: resource_usage_log.query added")


# ---------------------------------------------------------------------------
# Migration 2 — Carryover: seed project_resource_aspects from resource_aspects
# ---------------------------------------------------------------------------

def _m002_carryover(conn: sqlite3.Connection) -> None:
    """Backfill project_resource_aspects from legacy resource_aspects.

    For every resource that already has global aspect tags (resource_aspects),
    and that belongs to at least one project (via project_effective_members),
    insert a 'seed' row into project_resource_aspects so the new per-project
    layer is not empty for existing databases.

    INSERT OR IGNORE ensures idempotency: rows already written by the new
    code (source='llm', 'user', …) are left untouched.
    """
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "resource_aspects" not in tables or "project_resource_aspects" not in tables:
        log.debug("migrations: _m002_carryover skipped — tables not yet present")
        return

    conn.execute(
        """
        INSERT OR IGNORE INTO project_resource_aspects (
            project_id, resource_id, aspect_id,
            confidence, source, weight_signals_json,
            created_at, updated_at
        )
        SELECT
            pem.project_id,
            ra.resource_id,
            ra.aspect_id,
            ra.confidence,
            'seed',
            NULL,
            ra.created_at,
            ra.created_at
        FROM resource_aspects ra
        JOIN project_effective_members pem ON pem.node_id = ra.resource_id
        """
    )
    count = conn.execute("SELECT changes()").fetchone()[0]
    log.info("migrations: _m002_carryover seeded %d project_resource_aspects rows", count)


# ---------------------------------------------------------------------------
# Migration 3 — Aspect graph: aspect_edges + aspect_embeddings
# ---------------------------------------------------------------------------

def _m003_aspect_graph(conn: sqlite3.Connection) -> None:
    """Add aspect-to-aspect edge table and aspect embedding cache table.

    aspect_edges  — typed directed edges between aspect vocab entries
                    (parent_of, related_to, opposite_of, alias_of,
                     co_aspect_pmi, semantic_sim).
    aspect_embeddings — pre-computed embedding vectors for aspect labels.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS aspect_edges (
            id            TEXT PRIMARY KEY,
            src_aspect_id TEXT NOT NULL REFERENCES aspect_vocab(id) ON DELETE CASCADE,
            dst_aspect_id TEXT NOT NULL REFERENCES aspect_vocab(id) ON DELETE CASCADE,
            project_id    TEXT,
            edge_type     TEXT NOT NULL CHECK (edge_type IN (
                'parent_of','related_to','opposite_of','alias_of',
                'co_aspect_pmi','semantic_sim'
            )),
            weight        REAL NOT NULL DEFAULT 1.0,
            computed_at   TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_aspect_edges_triple
            ON aspect_edges(src_aspect_id, dst_aspect_id, edge_type,
                            COALESCE(project_id,''))
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_aspect_edges_src
            ON aspect_edges(src_aspect_id, edge_type)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_aspect_edges_project
            ON aspect_edges(project_id)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS aspect_embeddings (
            aspect_id    TEXT PRIMARY KEY REFERENCES aspect_vocab(id) ON DELETE CASCADE,
            embedding    BLOB NOT NULL,
            model_id     TEXT NOT NULL,
            computed_at  TEXT NOT NULL
        )
        """
    )
    log.info("migrations: aspect_edges and aspect_embeddings tables created")


# ---------------------------------------------------------------------------
# Migration 4 — Short IDs: add short_id column to raw_nodes + backfill
# ---------------------------------------------------------------------------

def _m004_short_id(conn: sqlite3.Connection) -> None:
    """Add raw_nodes.short_id column, unique index, and backfill existing rows.

    short_id = "rid_" + base32(sha256(id))[:6].lower()  — 10 chars total.
    """
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "raw_nodes" not in tables:
        log.debug("migrations: _m004_short_id skipped — raw_nodes not yet present")
        return

    cols = {row[1] for row in conn.execute("PRAGMA table_info(raw_nodes)")}
    if "short_id" not in cols:
        conn.execute("ALTER TABLE raw_nodes ADD COLUMN short_id TEXT")
        log.info("migrations: raw_nodes.short_id column added")

    # Unique index (WHERE short_id IS NOT NULL so NULLs don't conflict).
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_nodes_short_id
        ON raw_nodes(short_id) WHERE short_id IS NOT NULL
        """
    )

    # Backfill: compute short_id() for all rows that don't have one yet.
    from loci.graph.handles import short_id as _short_id

    rows = conn.execute(
        "SELECT id FROM raw_nodes WHERE short_id IS NULL"
    ).fetchall()

    batch: list[tuple[str, str]] = []
    for row in rows:
        rid = row[0]
        sid = _short_id(rid)
        batch.append((sid, rid))
        if len(batch) >= 500:
            conn.executemany(
                "UPDATE raw_nodes SET short_id = ? WHERE id = ?", batch
            )
            batch.clear()
    if batch:
        conn.executemany(
            "UPDATE raw_nodes SET short_id = ? WHERE id = ?", batch
        )

    log.info(
        "migrations: _m004_short_id backfilled %d raw_nodes rows", len(rows)
    )


# ---------------------------------------------------------------------------
# Migration 5 — Aspect provenance log
# ---------------------------------------------------------------------------

def _m005_aspect_provenance(conn: sqlite3.Connection) -> None:
    """Create the append-only aspect_provenance table.

    Records every add/remove/confirmed/rejected operation on aspect tags,
    with source, confidence, rationale, and session context.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS aspect_provenance (
            id           TEXT PRIMARY KEY,
            project_id   TEXT,
            resource_id  TEXT NOT NULL REFERENCES raw_nodes(id) ON DELETE CASCADE,
            aspect_id    TEXT NOT NULL REFERENCES aspect_vocab(id) ON DELETE CASCADE,
            action       TEXT NOT NULL CHECK (action IN ('added','removed','confirmed','rejected')),
            source       TEXT NOT NULL,
            confidence   REAL,
            rationale    TEXT,
            session_hash TEXT,
            recorded_at  TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ap_resource
        ON aspect_provenance(project_id, resource_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ap_aspect
        ON aspect_provenance(project_id, aspect_id)
        """
    )
    log.info("migrations: aspect_provenance table created")


# ---------------------------------------------------------------------------
# Migration registry — order matters, never reorder or delete entries
# ---------------------------------------------------------------------------

def _m006_aspect_dsl(conn: sqlite3.Connection) -> None:
    """v2.2 — CNL proposition columns on aspect_vocab + aspect_kinds + effective-confidence view.

    Adds structured DSL columns to aspect_vocab (topic, kind, role,
    target_aspect_id, modifiers_json), backfills topic = label for all
    existing rows, creates the aspect_kinds table, the supporting indexes,
    and the aspect_effective_confidence view.
    """
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "aspect_vocab" not in tables:
        return  # fresh install — DSL columns added by executescript (schema.sql)

    # Add DSL columns to existing aspect_vocab (idempotent via try/except)
    for col_def in [
        "topic TEXT",
        "kind TEXT",
        "role TEXT",
        "target_aspect_id TEXT",
        "modifiers_json TEXT",
    ]:
        try:
            conn.execute(f"ALTER TABLE aspect_vocab ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass  # column already exists

    # Backfill: for flat labels topic := label
    conn.execute("UPDATE aspect_vocab SET topic = label WHERE topic IS NULL")

    # Indexes (partial indexes may fail on older SQLite; fall back to full)
    for ddl in [
        "CREATE INDEX IF NOT EXISTS idx_av_topic ON aspect_vocab(topic, COALESCE(project_id, ''))",
        "CREATE INDEX IF NOT EXISTS idx_av_kind ON aspect_vocab(kind)",
        "CREATE INDEX IF NOT EXISTS idx_av_role ON aspect_vocab(role)",
        "CREATE INDEX IF NOT EXISTS idx_av_target ON aspect_vocab(target_aspect_id)",
    ]:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass

    # aspect_kinds table for extensible project-scoped kinds
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS aspect_kinds (
            kind        TEXT NOT NULL,
            project_id  TEXT,
            description TEXT,
            created_at  TEXT NOT NULL
        )
        """
    )
    try:
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_aspect_kinds_scope
            ON aspect_kinds(kind, COALESCE(project_id, ''))
            """
        )
    except sqlite3.OperationalError:
        pass

    # Seed global controlled kinds (source of truth is GLOBAL_KINDS in aspect_dsl)
    from loci.graph.aspect_dsl import GLOBAL_KINDS  # noqa: PLC0415
    from loci.graph.models import now_iso  # noqa: PLC0415
    ts = now_iso()
    for kind in GLOBAL_KINDS:
        conn.execute(
            "INSERT OR IGNORE INTO aspect_kinds(kind, project_id, created_at) VALUES (?, NULL, ?)",
            (kind, ts),
        )

    # aspect_effective_confidence view
    conn.execute("DROP VIEW IF EXISTS aspect_effective_confidence")
    conn.execute(
        """
        CREATE VIEW aspect_effective_confidence AS
        SELECT
            pra.project_id,
            pra.resource_id,
            pra.aspect_id,
            MIN(1.0, MAX(0.0,
                pra.confidence
                + 0.05 * COALESCE((
                    SELECT COUNT(*) FROM aspect_provenance p
                    WHERE p.project_id  = pra.project_id
                      AND p.resource_id = pra.resource_id
                      AND p.aspect_id   = pra.aspect_id
                      AND p.action      = 'confirmed'
                      AND p.source      = 'user'
                  ), 0)
                - 0.10 * COALESCE((
                    SELECT COUNT(*) FROM aspect_provenance p
                    WHERE p.project_id  = pra.project_id
                      AND p.resource_id = pra.resource_id
                      AND p.aspect_id   = pra.aspect_id
                      AND p.action      = 'rejected'
                  ), 0)
                + 0.05 * MIN(6, LOG(1.0 + COALESCE((
                    SELECT COUNT(*) FROM resource_usage_log u
                    WHERE u.project_id  = pra.project_id
                      AND u.resource_id = pra.resource_id
                      AND u.used_at    >= datetime('now', '-30 days')
                  ), 0)))
            )) AS effective_confidence
        FROM project_resource_aspects pra
        """
    )
    log.info("migrations: v2.2 DSL columns, aspect_kinds, effective_confidence view created")


def _m007_project_cwd(conn: sqlite3.Connection) -> None:
    """Add cwd column to projects for workspace-first MCP binding."""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "projects" not in tables:
        return  # fresh install — schema.sql will create it with the column
    cols = {row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
    if "cwd" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN cwd TEXT UNIQUE")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_projects_cwd ON projects(cwd) WHERE cwd IS NOT NULL"
    )


_MIGRATIONS: list[tuple[int, object]] = [
    (1, _m001_aspects_v2),
    (2, _m002_carryover),
    (3, _m003_aspect_graph),
    (4, _m004_short_id),
    (5, _m005_aspect_provenance),
    (6, _m006_aspect_dsl),
    (7, _m007_project_cwd),
]
