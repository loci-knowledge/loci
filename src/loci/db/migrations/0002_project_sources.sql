-- ============================================================================
-- 0002_project_sources.sql
--
-- Persisted source roots per project. PLAN allows files to live anywhere on
-- the user's filesystem; without this table, the user has to remember every
-- directory they want to (re-)scan. With it, `loci scan <project>` (no path)
-- walks all registered roots in one go.
--
-- A "source" here is just a directory or file path the user has registered
-- with a project. The walker still does the I/O; this table only records
-- *where to walk*. Files themselves remain content-addressed under
-- `raw_nodes`, deduped across projects.
-- ============================================================================

CREATE TABLE project_sources (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    root_path       TEXT NOT NULL,
    -- Free-form display label. Useful when one project pulls from "papers"
    -- and "obsidian" and "code"; the CLI shows the label in `source list`.
    label           TEXT,
    added_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_scanned_at TEXT,
    -- (project_id, root_path) is the natural key; PRIMARY KEY stays on `id`
    -- so we can refer to the row stably from the CLI/API.
    UNIQUE (project_id, root_path)
);

CREATE INDEX idx_project_sources_project ON project_sources(project_id);
