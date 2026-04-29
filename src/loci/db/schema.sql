-- ============================================================================
-- schema.sql — single canonical schema for loci v2.
--
-- Mental model:
--   Raw nodes are the only first-class content. Each raw is split into
--   ordered chunks (raw_chunks) for retrieval. Concept information lives in
--   aspect_vocab + resource_aspects + concept_edges and points at raw_nodes.
--
-- Tables:
--   nodes                       base shape (kind: raw only)
--   raw_nodes                   raw-specific columns
--   raw_chunks                  span-level slices of a raw's body
--   node_tags                   M:N free-form tags
--   projects                    project metadata + profile
--   project_membership          per-project pin/exclude/include overrides
--   information_workspaces      named bag of source roots, M:N with projects
--   workspace_sources           root paths owned by a workspace
--   workspace_membership        which raw nodes a workspace contains
--   project_workspaces          M:N: projects ↔ workspaces
--   project_effective_members   view: workspace ∪ override ∪ pinned
--   jobs                        background job queue
--   aspect_vocab                controlled aspect vocabulary
--   resource_aspects            M:N: raw resources ↔ aspects
--   concept_edges               typed directed edges between resources
--   resource_provenance         capture context per resource
--   resource_usage_log          audit trail of MCP/CLI reads
--   nodes_fts                   FTS5 mirror for raw titles/bodies/tags
--   node_vec                    sqlite-vec ANN index on raw embeddings
--   chunks_fts                  FTS5 mirror on chunk text
--   chunk_vec                   sqlite-vec ANN index on chunk embeddings
--
-- Conventions:
--   ULIDs: 26-char base32 ids. Timestamps: ISO-8601 UTC with milliseconds.
--   JSON columns validated with json_valid().
--   Everything is CREATE … IF NOT EXISTS so init_schema is idempotent.
-- ============================================================================


-- ---------------------------------------------------------------------------
-- nodes (base table; kind is restricted to 'raw' in v2)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS nodes (
    id                  TEXT PRIMARY KEY,
    kind                TEXT NOT NULL DEFAULT 'raw' CHECK (kind IN ('raw')),
    subkind             TEXT NOT NULL CHECK (subkind IN (
        'pdf','md','code','html','transcript','txt','image'
    )),
    title               TEXT NOT NULL,
    body                TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_accessed_at    TEXT,
    access_count        INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'live' CHECK (status IN (
        'live','stale','dismissed'
    ))
);

CREATE INDEX IF NOT EXISTS idx_nodes_kind        ON nodes(kind);
CREATE INDEX IF NOT EXISTS idx_nodes_subkind     ON nodes(subkind);
CREATE INDEX IF NOT EXISTS idx_nodes_status      ON nodes(status);
CREATE INDEX IF NOT EXISTS idx_nodes_updated_at  ON nodes(updated_at);
CREATE INDEX IF NOT EXISTS idx_nodes_last_access ON nodes(last_accessed_at);


-- ---------------------------------------------------------------------------
-- raw_nodes (kind = 'raw')
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_nodes (
    id              TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    -- sha256 hex truncated to 16 chars; full hash is the on-disk blob filename.
    content_hash    TEXT NOT NULL UNIQUE,
    canonical_path  TEXT NOT NULL,
    mime            TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL CHECK (size_bytes >= 0),
    -- 0 if the file at canonical_path is missing/deleted (audit pass).
    source_of_truth INTEGER NOT NULL DEFAULT 1 CHECK (source_of_truth IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_raw_canonical_path ON raw_nodes(canonical_path);


-- ---------------------------------------------------------------------------
-- raw_chunks (span-level slices for chunk-granular retrieval)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_chunks (
    id              TEXT PRIMARY KEY,
    raw_id          TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    ord             INTEGER NOT NULL,
    char_start      INTEGER NOT NULL CHECK (char_start >= 0),
    char_end        INTEGER NOT NULL CHECK (char_end >= char_start),
    text            TEXT NOT NULL,
    section         TEXT,
    UNIQUE (raw_id, ord)
);

CREATE INDEX IF NOT EXISTS idx_raw_chunks_raw ON raw_chunks(raw_id, ord);


-- ---------------------------------------------------------------------------
-- node_tags (free-form tags; seed for the aspect system)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS node_tags (
    node_id     TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    tag         TEXT NOT NULL,
    PRIMARY KEY (node_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_node_tags_tag ON node_tags(tag);


-- ---------------------------------------------------------------------------
-- projects
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projects (
    id              TEXT PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    profile_md      TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_active_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    config          TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(config))
);


-- ---------------------------------------------------------------------------
-- project_membership
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS project_membership (
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    node_id     TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'included' CHECK (role IN (
        'included','excluded','pinned'
    )),
    added_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    added_by    TEXT NOT NULL DEFAULT 'user',
    PRIMARY KEY (project_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_membership_project ON project_membership(project_id, role);
CREATE INDEX IF NOT EXISTS idx_membership_node    ON project_membership(node_id);


-- ---------------------------------------------------------------------------
-- information_workspaces
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS information_workspaces (
    id              TEXT PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    description_md  TEXT NOT NULL DEFAULT '',
    kind            TEXT NOT NULL DEFAULT 'mixed' CHECK (kind IN (
        'papers','codebase','notes','transcripts','web','mixed'
    )),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_active_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_scanned_at TEXT,
    config          TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(config))
);

CREATE INDEX IF NOT EXISTS idx_workspaces_slug ON information_workspaces(slug);


-- ---------------------------------------------------------------------------
-- workspace_sources
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workspace_sources (
    id              TEXT PRIMARY KEY,
    workspace_id    TEXT NOT NULL REFERENCES information_workspaces(id) ON DELETE CASCADE,
    root_path       TEXT NOT NULL,
    label           TEXT,
    added_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_scanned_at TEXT,
    UNIQUE (workspace_id, root_path)
);

CREATE INDEX IF NOT EXISTS idx_workspace_sources_workspace ON workspace_sources(workspace_id);


-- ---------------------------------------------------------------------------
-- workspace_membership
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workspace_membership (
    workspace_id    TEXT NOT NULL REFERENCES information_workspaces(id) ON DELETE CASCADE,
    node_id         TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    added_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (workspace_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_workspace_membership_workspace ON workspace_membership(workspace_id);
CREATE INDEX IF NOT EXISTS idx_workspace_membership_node      ON workspace_membership(node_id);


-- ---------------------------------------------------------------------------
-- project_workspaces (M:N)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS project_workspaces (
    project_id              TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    workspace_id            TEXT NOT NULL REFERENCES information_workspaces(id) ON DELETE CASCADE,
    linked_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    role                    TEXT NOT NULL DEFAULT 'reference' CHECK (role IN (
        'primary','reference','excluded'
    )),
    weight                  REAL NOT NULL DEFAULT 1.0 CHECK (weight BETWEEN 0.0 AND 1.0),
    last_relevance_pass_at  TEXT,
    PRIMARY KEY (project_id, workspace_id)
);

CREATE INDEX IF NOT EXISTS idx_project_workspaces_project   ON project_workspaces(project_id);
CREATE INDEX IF NOT EXISTS idx_project_workspaces_workspace ON project_workspaces(workspace_id);


-- ---------------------------------------------------------------------------
-- project_effective_members  (view: workspace ∪ override ∪ pinned)
-- ---------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS project_effective_members AS
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
SELECT pm.project_id,
       pm.node_id,
       'pinned' AS source
FROM   project_membership pm
WHERE  pm.role = 'pinned';


-- ---------------------------------------------------------------------------
-- jobs (background queue)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN (
        'classify_aspects','parse_links','log_usage','embed_missing'
    )),
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
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_project        ON jobs(project_id);
CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint    ON jobs(fingerprint) WHERE fingerprint IS NOT NULL;


-- ---------------------------------------------------------------------------
-- aspect_vocab (controlled vocabulary; auto-grows)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS aspect_vocab (
    id                       TEXT PRIMARY KEY,
    label                    TEXT NOT NULL UNIQUE,
    description              TEXT,
    -- Optional ConceptNet relation hint (IsA, UsedFor, PartOf, RelatedTo, …).
    conceptnet_relation_hint TEXT,
    user_defined             INTEGER NOT NULL DEFAULT 1,
    auto_inferred            INTEGER NOT NULL DEFAULT 0,
    last_used                TEXT,
    created_at               TEXT NOT NULL
);


-- ---------------------------------------------------------------------------
-- resource_aspects (M:N: raw resources ↔ aspects)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS resource_aspects (
    resource_id TEXT NOT NULL REFERENCES raw_nodes(id) ON DELETE CASCADE,
    aspect_id   TEXT NOT NULL REFERENCES aspect_vocab(id) ON DELETE CASCADE,
    confidence  REAL NOT NULL DEFAULT 1.0,
    source      TEXT NOT NULL CHECK (source IN ('user','folder','inferred','usage')),
    created_at  TEXT NOT NULL,
    PRIMARY KEY (resource_id, aspect_id)
);

CREATE INDEX IF NOT EXISTS idx_resource_aspects_resource ON resource_aspects(resource_id);
CREATE INDEX IF NOT EXISTS idx_resource_aspects_aspect   ON resource_aspects(aspect_id);
CREATE INDEX IF NOT EXISTS idx_resource_aspects_source   ON resource_aspects(source);


-- ---------------------------------------------------------------------------
-- concept_edges (typed directed edges between resources)
-- ---------------------------------------------------------------------------
-- edge_type values: cites | wikilink | co_aspect | co_folder | custom
-- relation_hint optionally borrows a ConceptNet label.
CREATE TABLE IF NOT EXISTS concept_edges (
    id            TEXT PRIMARY KEY,
    src_id        TEXT NOT NULL REFERENCES raw_nodes(id) ON DELETE CASCADE,
    dst_id        TEXT NOT NULL REFERENCES raw_nodes(id) ON DELETE CASCADE,
    edge_type     TEXT NOT NULL,
    relation_hint TEXT,
    weight        REAL NOT NULL DEFAULT 1.0,
    metadata_json TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_concept_edges_src  ON concept_edges(src_id);
CREATE INDEX IF NOT EXISTS idx_concept_edges_dst  ON concept_edges(dst_id);
CREATE INDEX IF NOT EXISTS idx_concept_edges_type ON concept_edges(edge_type);


-- ---------------------------------------------------------------------------
-- resource_provenance (capture context per resource)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS resource_provenance (
    resource_id  TEXT PRIMARY KEY REFERENCES raw_nodes(id) ON DELETE CASCADE,
    source_url   TEXT,
    folder       TEXT,
    saved_via    TEXT NOT NULL DEFAULT 'cli',  -- cli | mcp | watch
    context_text TEXT,
    captured_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_provenance_folder ON resource_provenance(folder);


-- ---------------------------------------------------------------------------
-- resource_usage_log (append-only audit of MCP/CLI reads)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS resource_usage_log (
    id             TEXT PRIMARY KEY,
    resource_id    TEXT NOT NULL REFERENCES raw_nodes(id) ON DELETE CASCADE,
    session_hash   TEXT,
    tool_call_type TEXT,
    context_note   TEXT,
    used_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_log_resource ON resource_usage_log(resource_id);
CREATE INDEX IF NOT EXISTS idx_usage_log_session  ON resource_usage_log(session_hash);


-- ---------------------------------------------------------------------------
-- nodes_fts (FTS5 mirror — raw titles/bodies/tags)
-- ---------------------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    node_id UNINDEXED,
    title,
    body,
    tags,
    tokenize = 'porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS nodes_ai AFTER INSERT ON nodes BEGIN
    INSERT INTO nodes_fts(node_id, title, body, tags) VALUES (
        new.id, new.title, new.body,
        COALESCE((SELECT group_concat(tag, ' ') FROM node_tags WHERE node_id = new.id), '')
    );
END;

CREATE TRIGGER IF NOT EXISTS nodes_ad AFTER DELETE ON nodes BEGIN
    DELETE FROM nodes_fts WHERE node_id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS nodes_au AFTER UPDATE OF title, body ON nodes BEGIN
    DELETE FROM nodes_fts WHERE node_id = old.id;
    INSERT INTO nodes_fts(node_id, title, body, tags) VALUES (
        new.id, new.title, new.body,
        COALESCE((SELECT group_concat(tag, ' ') FROM node_tags WHERE node_id = new.id), '')
    );
END;

CREATE TRIGGER IF NOT EXISTS tags_ai AFTER INSERT ON node_tags BEGIN
    DELETE FROM nodes_fts WHERE node_id = new.node_id;
    INSERT INTO nodes_fts(node_id, title, body, tags) SELECT
        n.id, n.title, n.body,
        COALESCE((SELECT group_concat(tag, ' ') FROM node_tags WHERE node_id = n.id), '')
    FROM nodes n WHERE n.id = new.node_id;
END;

CREATE TRIGGER IF NOT EXISTS tags_ad AFTER DELETE ON node_tags BEGIN
    DELETE FROM nodes_fts WHERE node_id = old.node_id;
    INSERT INTO nodes_fts(node_id, title, body, tags) SELECT
        n.id, n.title, n.body,
        COALESCE((SELECT group_concat(tag, ' ') FROM node_tags WHERE node_id = n.id), '')
    FROM nodes n WHERE n.id = old.node_id;
END;


-- ---------------------------------------------------------------------------
-- node_vec (sqlite-vec ANN — raw embeddings, 384-dim)
-- ---------------------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS node_vec USING vec0(
    node_id TEXT PRIMARY KEY,
    embedding FLOAT[384]
);


-- ---------------------------------------------------------------------------
-- chunks_fts (FTS5 mirror — chunk text)
-- ---------------------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    raw_id UNINDEXED,
    text,
    section,
    tokenize = 'porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS raw_chunks_ai AFTER INSERT ON raw_chunks BEGIN
    INSERT INTO chunks_fts(chunk_id, raw_id, text, section)
    VALUES (new.id, new.raw_id, new.text, COALESCE(new.section, ''));
END;

CREATE TRIGGER IF NOT EXISTS raw_chunks_ad AFTER DELETE ON raw_chunks BEGIN
    DELETE FROM chunks_fts WHERE chunk_id = old.id;
    -- chunk_vec is a vec0 virtual table; FK cascade does not reach it.
    DELETE FROM chunk_vec WHERE chunk_id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS raw_chunks_au AFTER UPDATE OF text, section ON raw_chunks BEGIN
    DELETE FROM chunks_fts WHERE chunk_id = old.id;
    INSERT INTO chunks_fts(chunk_id, raw_id, text, section)
    VALUES (new.id, new.raw_id, new.text, COALESCE(new.section, ''));
END;


-- ---------------------------------------------------------------------------
-- chunk_vec (sqlite-vec ANN — chunk embeddings, 384-dim)
-- ---------------------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vec USING vec0(
    chunk_id TEXT PRIMARY KEY,
    embedding FLOAT[384]
);
