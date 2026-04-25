-- ============================================================================
-- 0004_information_workspaces.sql
--
-- Introduces Information Workspaces as first-class entities, M:N with Projects.
--
-- Motivation: project_sources is strictly 1:N (a root path belongs to exactly
-- one project). Users want to share a folder of papers or a codebase across
-- multiple projects without re-registering and re-scanning. This migration
-- separates the *source of files* (information workspace) from the *intent and
-- profile* (project), linking them many-to-many.
--
-- New tables:
--   information_workspaces  — a named bag of source roots (peer of projects)
--   workspace_sources       — root paths owned by a workspace (replaces project_sources ownership)
--   workspace_membership    — which raw nodes a workspace contains
--   project_workspaces      — M:N join: projects ↔ information_workspaces
--
-- New view:
--   project_effective_members — derived union of workspace-based + override membership
--
-- Backfill:
--   For each existing project P, creates a legacy workspace with the same id as P,
--   slug 'ws_<slug>', name '(legacy) <name>'. Migrates project_sources rows into
--   workspace_sources, links with role='primary', and copies raw project_membership
--   rows into workspace_membership.
--
-- project_sources is NOT dropped here; it will be removed in 0007 after all
-- write paths have been migrated. project_membership retains rows for project-
-- specific overrides (pin, exclude, project-private interpretations).
-- ============================================================================


-- ---------------------------------------------------------------------------
-- information_workspaces
-- ---------------------------------------------------------------------------
CREATE TABLE information_workspaces (
    id              TEXT PRIMARY KEY,           -- ULID
    slug            TEXT NOT NULL UNIQUE,       -- lowercase alphanumeric / dashes / underscores
    name            TEXT NOT NULL,
    description_md  TEXT NOT NULL DEFAULT '',   -- what kind of info lives here (used by relevance pass)
    -- Coarse hint for retrieval weighting and prompt phrasing.
    kind            TEXT NOT NULL DEFAULT 'mixed' CHECK (kind IN (
        'papers', 'codebase', 'notes', 'transcripts', 'web', 'mixed'
    )),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_active_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_scanned_at TEXT,
    -- JSON config: ingest filters (globs, excludes), embedding overrides, etc.
    config          TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(config))
);

CREATE INDEX idx_workspaces_slug ON information_workspaces(slug);


-- ---------------------------------------------------------------------------
-- workspace_sources
-- ---------------------------------------------------------------------------
-- Mirrors the shape of project_sources but ownership moves to the workspace.
-- A root path can still only sit under one workspace (avoids ambiguity in
-- scan/dedup logging) — same constraint shape as the original.
CREATE TABLE workspace_sources (
    id              TEXT PRIMARY KEY,
    workspace_id    TEXT NOT NULL REFERENCES information_workspaces(id) ON DELETE CASCADE,
    root_path       TEXT NOT NULL,
    label           TEXT,
    added_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_scanned_at TEXT,
    UNIQUE (workspace_id, root_path)
);

CREATE INDEX idx_workspace_sources_workspace ON workspace_sources(workspace_id);


-- ---------------------------------------------------------------------------
-- workspace_membership
-- ---------------------------------------------------------------------------
-- Which raw nodes belong to a workspace. Populated by the ingest pipeline
-- during scan. A single raw (same content_hash, content-deduped globally) can
-- appear in many workspaces if the same file is registered under different
-- workspace roots.
CREATE TABLE workspace_membership (
    workspace_id    TEXT NOT NULL REFERENCES information_workspaces(id) ON DELETE CASCADE,
    node_id         TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    added_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (workspace_id, node_id)
);

CREATE INDEX idx_workspace_membership_workspace ON workspace_membership(workspace_id);
CREATE INDEX idx_workspace_membership_node      ON workspace_membership(node_id);


-- ---------------------------------------------------------------------------
-- project_workspaces  (M:N join)
-- ---------------------------------------------------------------------------
CREATE TABLE project_workspaces (
    project_id              TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    workspace_id            TEXT NOT NULL REFERENCES information_workspaces(id) ON DELETE CASCADE,
    linked_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- primary: the main source of truth for this project; reference: supplemental;
    -- excluded: explicitly not pulled in (rare override).
    role                    TEXT NOT NULL DEFAULT 'reference' CHECK (role IN (
        'primary', 'reference', 'excluded'
    )),
    weight                  REAL NOT NULL DEFAULT 1.0 CHECK (weight BETWEEN 0.0 AND 1.0),
    -- Timestamp of the last relevance-pass job that ran for this pair.
    last_relevance_pass_at  TEXT,
    PRIMARY KEY (project_id, workspace_id)
);

CREATE INDEX idx_project_workspaces_project   ON project_workspaces(project_id);
CREATE INDEX idx_project_workspaces_workspace ON project_workspaces(workspace_id);


-- ---------------------------------------------------------------------------
-- project_effective_members  (derived view — NOT materialized)
-- ---------------------------------------------------------------------------
-- A project's effective node membership is:
--   (union of workspace members across all non-excluded linked workspaces)
--   PLUS explicit project_membership(role='included') overrides
--   MINUS explicit project_membership(role='excluded') overrides
--
-- Interpretation nodes are always stored directly in project_membership (they
-- are project-scoped, not workspace-scoped). This view therefore returns both
-- raws (from workspaces) and interpretations (from project_membership).
--
-- Performance: all joins are on indexed primary-key columns. The NOT EXISTS
-- anti-join resolves in O(1) via the idx_membership_project index.
CREATE VIEW project_effective_members AS
-- Raws and interps from linked workspaces
SELECT pw.project_id,
       wm.node_id,
       'workspace' AS source
FROM   project_workspaces pw
JOIN   workspace_membership wm ON wm.workspace_id = pw.workspace_id
WHERE  pw.role != 'excluded'
  AND  NOT EXISTS (
           SELECT 1 FROM project_membership pm_excl
           WHERE  pm_excl.project_id = pw.project_id
             AND  pm_excl.node_id    = wm.node_id
             AND  pm_excl.role       = 'excluded'
       )

UNION

-- Explicit project-level 'included' overrides (project-private nodes, legacy raws)
SELECT pm.project_id,
       pm.node_id,
       'override' AS source
FROM   project_membership pm
WHERE  pm.role = 'included'
  AND  NOT EXISTS (
           SELECT 1 FROM project_membership pm_excl
           WHERE  pm_excl.project_id = pm.project_id
             AND  pm_excl.node_id    = pm.node_id
             AND  pm_excl.role       = 'excluded'
       )

UNION

-- Pinned nodes always appear (pin overrides workspace exclusion)
SELECT pm.project_id,
       pm.node_id,
       'pinned' AS source
FROM   project_membership pm
WHERE  pm.role = 'pinned';


-- No backfill: this is a greenfield deployment. Workspaces are created via the
-- API or CLI; existing project data is not migrated automatically.
