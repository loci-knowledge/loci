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
    -- kind is validated in Python (jobs/__init__.py); no SQL CHECK so new
    -- kinds don't require a schema migration.
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
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_project        ON jobs(project_id);
CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint    ON jobs(fingerprint) WHERE fingerprint IS NOT NULL;


-- ---------------------------------------------------------------------------
-- aspect_vocab (controlled vocabulary; auto-grows)
-- ---------------------------------------------------------------------------
-- project_id = NULL means a global concept shared across all projects.
-- project_id = <id> means a label local to that project only.
-- Uniqueness is enforced on (label, COALESCE(project_id, '')) so the same
-- word can exist as both a global label and a project-local refinement.
CREATE TABLE IF NOT EXISTS aspect_vocab (
    id                       TEXT PRIMARY KEY,
    label                    TEXT NOT NULL,
    project_id               TEXT,
    description              TEXT,
    -- Optional ConceptNet relation hint (IsA, UsedFor, PartOf, RelatedTo, …).
    conceptnet_relation_hint TEXT,
    user_defined             INTEGER NOT NULL DEFAULT 1,
    auto_inferred            INTEGER NOT NULL DEFAULT 0,
    last_used                TEXT,
    created_at               TEXT NOT NULL,
    -- DSL proposition fields (v2.2) — all nullable for backward compat.
    -- `topic` is the head slug; for flat labels topic = label.
    -- `kind` is from the controlled+extensible set (methodology, technique, …).
    -- `role` is from the CLOSED set (critiques, extends, …); drives aspect_edges.
    -- `target_aspect_id` is the FK to the target vocab row when role is set.
    -- `modifiers_json` holds arbitrary key=value pairs as JSON.
    topic                    TEXT,
    kind                     TEXT,
    role                     TEXT,
    target_aspect_id         TEXT REFERENCES aspect_vocab(id) ON DELETE SET NULL,
    modifiers_json           TEXT CHECK (modifiers_json IS NULL OR json_valid(modifiers_json))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_aspect_vocab_label_scope
    ON aspect_vocab(label, COALESCE(project_id, ''));
CREATE INDEX IF NOT EXISTS idx_av_topic   ON aspect_vocab(topic, COALESCE(project_id, ''));
CREATE INDEX IF NOT EXISTS idx_av_kind    ON aspect_vocab(kind) WHERE kind IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_av_role    ON aspect_vocab(role) WHERE role IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_av_target  ON aspect_vocab(target_aspect_id) WHERE target_aspect_id IS NOT NULL;

-- Extensible per-project kind vocabulary (v2.2)
CREATE TABLE IF NOT EXISTS aspect_kinds (
    kind        TEXT NOT NULL,
    project_id  TEXT,           -- NULL = global controlled set
    description TEXT,
    created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_aspect_kinds_scope
    ON aspect_kinds(kind, COALESCE(project_id, ''));

-- Effective-confidence view: adjusts pra.confidence by provenance actions + usage (v2.2)
CREATE VIEW IF NOT EXISTS aspect_effective_confidence AS
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
FROM project_resource_aspects pra;

-- ---------------------------------------------------------------------------
-- aspect_edges (typed directed edges between aspect vocab entries)
-- ---------------------------------------------------------------------------
-- edge_type values:
--   parent_of    — broader concept hierarchy (e.g. deep-learning → parent_of → transformer)
--   related_to   — soft semantic relation
--   opposite_of  — antonyms / contrasting concepts
--   alias_of     — synonyms / alternate spellings
--   co_aspect_pmi — PMI-weighted co-occurrence within a project
--   semantic_sim  — cosine similarity between label embeddings
-- project_id = NULL → global edge; project_id = <id> → project-local edge
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
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_aspect_edges_triple
    ON aspect_edges(src_aspect_id, dst_aspect_id, edge_type,
                    COALESCE(project_id,''));
CREATE INDEX IF NOT EXISTS idx_aspect_edges_src
    ON aspect_edges(src_aspect_id, edge_type);
CREATE INDEX IF NOT EXISTS idx_aspect_edges_project
    ON aspect_edges(project_id);


-- ---------------------------------------------------------------------------
-- aspect_embeddings (pre-computed label vectors for cosine matching)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS aspect_embeddings (
    aspect_id    TEXT PRIMARY KEY REFERENCES aspect_vocab(id) ON DELETE CASCADE,
    embedding    BLOB NOT NULL,
    model_id     TEXT NOT NULL,
    computed_at  TEXT NOT NULL
);


-- ---------------------------------------------------------------------------
-- resource_aspects (M:N: raw resources ↔ aspects)  [legacy global table]
-- ---------------------------------------------------------------------------
-- New code writes to project_resource_aspects instead. This table is kept
-- as a backfill seed during the dual-write phase and will be retired.
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
-- project_resource_aspects (per-project interpretation of a resource)
-- ---------------------------------------------------------------------------
-- Same resource can carry different aspects across different projects,
-- reflecting how each project "reads" the same content.
-- source values:
--   user         — hand-labelled by the user (gold; never overwritten by LLM)
--   folder       — inherited from workspace folder path
--   inferred     — KeyBERT / heuristic ingest-time suggestion
--   usage        — promoted by repeated retrieval access
--   llm          — produced by the infer_interpretation background job
--   conversation — inferred from Claude Code conversation hook events
--   seed         — backfilled from legacy resource_aspects during migration
CREATE TABLE IF NOT EXISTS project_resource_aspects (
    project_id          TEXT NOT NULL,
    resource_id         TEXT NOT NULL REFERENCES raw_nodes(id) ON DELETE CASCADE,
    aspect_id           TEXT NOT NULL REFERENCES aspect_vocab(id) ON DELETE CASCADE,
    confidence          REAL NOT NULL DEFAULT 1.0,
    source              TEXT NOT NULL CHECK (source IN (
        'user','folder','inferred','usage','llm','conversation','seed'
    )),
    weight_signals_json TEXT CHECK (weight_signals_json IS NULL OR json_valid(weight_signals_json)),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (project_id, resource_id, aspect_id)
);

CREATE INDEX IF NOT EXISTS idx_pra_project_aspect ON project_resource_aspects(project_id, aspect_id);
CREATE INDEX IF NOT EXISTS idx_pra_resource        ON project_resource_aspects(resource_id);
CREATE INDEX IF NOT EXISTS idx_pra_source          ON project_resource_aspects(source);


-- ---------------------------------------------------------------------------
-- project_interpretations (LLM narrative per project × resource)
-- ---------------------------------------------------------------------------
-- A short natural-language interpretation of a resource from a project's
-- vantage point, generated by the infer_interpretation job.
-- inputs_hash gates re-generation: skip if unchanged.
CREATE TABLE IF NOT EXISTS project_interpretations (
    project_id   TEXT NOT NULL,
    resource_id  TEXT NOT NULL REFERENCES raw_nodes(id) ON DELETE CASCADE,
    summary_md   TEXT NOT NULL,
    stance       TEXT,   -- methodological|supporting|contradictory|reference|tangential
    inputs_hash  TEXT NOT NULL,
    model_id     TEXT,
    generated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, resource_id)
);


-- ---------------------------------------------------------------------------
-- concept_edges (typed directed edges between resources)
-- ---------------------------------------------------------------------------
-- Structural edge_type values: cites | wikilink | co_aspect | co_folder | custom
-- Semantic (project-level) edge types: supports | contradicts | instantiates |
--   depends_on | co_recalled | addresses_query
-- relation_hint optionally borrows a ConceptNet label.
-- project_id = NULL → global edge (fact about the document, e.g. citation).
-- project_id = <id> → interpretation edge local to that project.
CREATE TABLE IF NOT EXISTS concept_edges (
    id            TEXT PRIMARY KEY,
    src_id        TEXT NOT NULL REFERENCES raw_nodes(id) ON DELETE CASCADE,
    dst_id        TEXT NOT NULL REFERENCES raw_nodes(id) ON DELETE CASCADE,
    edge_type     TEXT NOT NULL,
    relation_hint TEXT,
    weight        REAL NOT NULL DEFAULT 1.0,
    metadata      TEXT,
    project_id    TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_concept_edges_src     ON concept_edges(src_id);
CREATE INDEX IF NOT EXISTS idx_concept_edges_dst     ON concept_edges(dst_id);
CREATE INDEX IF NOT EXISTS idx_concept_edges_type    ON concept_edges(edge_type);
CREATE INDEX IF NOT EXISTS idx_concept_edges_project ON concept_edges(project_id);


-- ---------------------------------------------------------------------------
-- aspect_provenance (append-only log of aspect add/remove events)
-- ---------------------------------------------------------------------------
-- Tracks every aspect tag operation: who applied it, why, with what confidence.
-- action values: added | removed | confirmed | rejected
-- source mirrors resource_aspects.source plus 'user', 'llm', etc.
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
);

CREATE INDEX IF NOT EXISTS idx_ap_resource ON aspect_provenance(project_id, resource_id);
CREATE INDEX IF NOT EXISTS idx_ap_aspect   ON aspect_provenance(project_id, aspect_id);


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
    project_id     TEXT,
    session_hash   TEXT,
    tool_call_type TEXT,
    query          TEXT,
    context_note   TEXT,
    used_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_log_resource ON resource_usage_log(resource_id);
CREATE INDEX IF NOT EXISTS idx_usage_log_session  ON resource_usage_log(session_hash);
CREATE INDEX IF NOT EXISTS idx_usage_log_project  ON resource_usage_log(project_id, used_at);


-- ---------------------------------------------------------------------------
-- conversation_events (Claude Code hook signal source)
-- ---------------------------------------------------------------------------
-- Populated by `loci event conversation` CLI subcommand invoked via hooks.
-- Feeds passive project-scoped inference without the user calling loci tools.
CREATE TABLE IF NOT EXISTS conversation_events (
    id           TEXT PRIMARY KEY,
    project_id   TEXT,
    session_hash TEXT,
    role         TEXT NOT NULL CHECK (role IN ('user','assistant')),
    text         TEXT NOT NULL,
    cwd          TEXT,
    received_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conv_project_time ON conversation_events(project_id, received_at);
CREATE INDEX IF NOT EXISTS idx_conv_session      ON conversation_events(session_hash);


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
