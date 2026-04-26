-- ============================================================================
-- 0001_initial.sql — canonical schema for the loci DAG
--
-- This is the single source of truth for the database shape. The project does
-- not maintain backward-compatible migrations: when the schema changes, this
-- file is rewritten and existing databases are reset (`loci reset`). The
-- `_migrations` table tracks application of this file alone.
--
-- Mental model (DAG):
--   raw nodes are *leaves*; they store source-of-truth content.
--   interpretation nodes are *inner nodes*; they are the user's "loci of
--   thought" — pointers that route a query to the parts of raw sources that
--   matter for this project. An interpretation never holds the answer; it
--   tells you which raws to read and why.
--
-- Topology rules (enforced at the app layer, mirrored as CHECK where possible):
--   - cites:        src.kind = interpretation, dst.kind = raw  (the locus → source pointer)
--   - derives_from: src.kind = interpretation, dst.kind = interpretation
--                   directed; cycle detection in EdgeRepository
--   - raw nodes have NO outgoing edges (raws are pure leaves)
--   - no symmetric edges, no inverses
--
-- Layout:
--   nodes                       base shape (kind: raw|interpretation)
--   raw_nodes                   raw-specific columns
--   interpretation_nodes        interp-specific columns (angle, rationale, source_anchor)
--   node_tags                   many-to-many tags
--   edges                       directed DAG edges
--   projects                    project metadata + profile
--   project_membership          per-project pin/exclude/include overrides
--   information_workspaces      named bag of source roots, M:N with projects
--   workspace_sources           root paths owned by a workspace
--   workspace_membership        which raw nodes a workspace contains
--   project_workspaces          M:N join: projects ↔ workspaces
--   project_effective_members   view: workspace ∪ override ∪ pinned
--   responses                   one row per retrieve/draft/q call
--   traces                      per-node provenance per response
--   proposals                   queue for absorb-detected items awaiting accept/dismiss
--   jobs                        background job queue
--   agent_reflections           audit log of every interpreter reflection cycle
--   communities                 community-detection snapshots
--   nodes_fts                   FTS5 mirror for lexical retrieval
--   node_vec                    sqlite-vec ANN index
--
-- Conventions:
--   ULIDs: 26-char base32 ids. Timestamps: ISO-8601 UTC with milliseconds.
--   JSON columns validated with json_valid().
-- ============================================================================


-- ---------------------------------------------------------------------------
-- nodes (base table)
-- ---------------------------------------------------------------------------
CREATE TABLE nodes (
    id                  TEXT PRIMARY KEY,
    kind                TEXT NOT NULL CHECK (kind IN ('raw','interpretation')),
    -- Subkind union: raw subkinds are file-format hints; interpretation
    -- subkinds are framings of the locus.
    subkind             TEXT NOT NULL CHECK (subkind IN (
        'pdf','md','code','html','transcript','txt','image',
        'philosophy','tension','decision','relevance'
    )),
    title               TEXT NOT NULL,
    body                TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_accessed_at    TEXT,
    access_count        INTEGER NOT NULL DEFAULT 0,
    confidence          REAL NOT NULL DEFAULT 1.0
                            CHECK (confidence >= 0.0 AND confidence <= 1.0),
    status              TEXT NOT NULL DEFAULT 'live' CHECK (status IN (
        'proposed','live','dirty','stale','dismissed'
    ))
);

CREATE INDEX idx_nodes_kind         ON nodes(kind);
CREATE INDEX idx_nodes_subkind      ON nodes(subkind);
CREATE INDEX idx_nodes_status       ON nodes(status);
CREATE INDEX idx_nodes_updated_at   ON nodes(updated_at);
CREATE INDEX idx_nodes_last_access  ON nodes(last_accessed_at);


-- ---------------------------------------------------------------------------
-- raw_nodes  (kind = 'raw')
-- ---------------------------------------------------------------------------
CREATE TABLE raw_nodes (
    node_id             TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    -- sha256 of content, hex, truncated to 16 chars. Full hash is the on-disk
    -- blob filename; this short hash is the unique key.
    content_hash        TEXT NOT NULL UNIQUE,
    canonical_path      TEXT NOT NULL,
    mime                TEXT NOT NULL,
    size_bytes          INTEGER NOT NULL CHECK (size_bytes >= 0),
    -- 0 if the file at canonical_path is missing/deleted (audit pass).
    source_of_truth     INTEGER NOT NULL DEFAULT 1 CHECK (source_of_truth IN (0,1))
);

CREATE INDEX idx_raw_canonical_path ON raw_nodes(canonical_path);


-- ---------------------------------------------------------------------------
-- interpretation_nodes  (kind = 'interpretation')
-- ---------------------------------------------------------------------------
-- Each interpretation is a *locus of thought* — a marker that says "this
-- region of source-space matters to this project at this angle." Three slots:
--   relation_md     — how the source(s) relate to the project (1–3 sentences)
--   overlap_md      — where the source and the project intersect (concrete)
--   source_anchor_md— which part(s) of the cited source(s) carry the weight
--                     (quote, section reference, line range, function name…)
-- These are NOT optional content; the body field is unused for agent-written
-- interpretations and only shown to the LLM as routing context.
--
-- angle: closed-vocabulary tag (relevance subkind only); NULL otherwise.
-- rationale_md: legacy field, kept for proposal-acceptance compatibility.
CREATE TABLE interpretation_nodes (
    node_id                 TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    origin                  TEXT NOT NULL CHECK (origin IN (
        'user_correction','user_pin','user_summary',
        'user_explicit_create','proposal_accepted','agent_synthesis'
    )),
    origin_session_id       TEXT,
    origin_response_id      TEXT REFERENCES responses(id) ON DELETE SET NULL,
    -- The three locus slots:
    relation_md             TEXT NOT NULL DEFAULT '',
    overlap_md              TEXT NOT NULL DEFAULT '',
    source_anchor_md        TEXT NOT NULL DEFAULT '',
    -- Closed-vocab tag for relevance subkind:
    angle                   TEXT,
    -- Legacy/free-form rationale (kept for compatibility with proposal flow):
    rationale_md            TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_interp_origin ON interpretation_nodes(origin);
CREATE INDEX idx_interp_angle  ON interpretation_nodes(angle);


-- ---------------------------------------------------------------------------
-- node_tags
-- ---------------------------------------------------------------------------
CREATE TABLE node_tags (
    node_id     TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    tag         TEXT NOT NULL,
    PRIMARY KEY (node_id, tag)
);

CREATE INDEX idx_node_tags_tag ON node_tags(tag);


-- ---------------------------------------------------------------------------
-- edges  (DAG)
-- ---------------------------------------------------------------------------
-- Two directed edge types; no symmetric edges, no inverses.
--   cites        interp → raw   ("this locus points at this source")
--   derives_from interp → interp ("this locus builds on that one")
--
-- The raw-leaf rule (raws have no outgoing edges) is enforced in
-- EdgeRepository: every insert validates src.kind / dst.kind against the
-- edge type and runs cycle detection on derives_from.
CREATE TABLE edges (
    id                  TEXT PRIMARY KEY,
    src                 TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    dst                 TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    type                TEXT NOT NULL CHECK (type IN ('cites','derives_from')),
    weight              REAL NOT NULL DEFAULT 1.0
                            CHECK (weight >= 0.0 AND weight <= 1.0),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_by          TEXT NOT NULL DEFAULT 'user' CHECK (created_by IN (
        'user','system','proposal_accepted'
    )),
    -- Per-edge rationale: for cites edges, the snippet/quote/why-this-section.
    -- For derives_from edges, the inheritance reason.
    rationale           TEXT,
    -- For cites edges in relevance interps, the angle is denormalised here for
    -- direct query. NULL otherwise.
    angle               TEXT,
    UNIQUE (src, dst, type),
    CHECK (src <> dst)
);

CREATE INDEX idx_edges_src      ON edges(src, type);
CREATE INDEX idx_edges_dst      ON edges(dst, type);
CREATE INDEX idx_edges_type     ON edges(type);


-- ---------------------------------------------------------------------------
-- projects
-- ---------------------------------------------------------------------------
CREATE TABLE projects (
    id                  TEXT PRIMARY KEY,
    slug                TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    profile_md          TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_active_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    config              TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(config))
);


-- ---------------------------------------------------------------------------
-- project_membership
-- ---------------------------------------------------------------------------
CREATE TABLE project_membership (
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    node_id             TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    role                TEXT NOT NULL DEFAULT 'included' CHECK (role IN (
        'included','excluded','pinned'
    )),
    added_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    added_by            TEXT NOT NULL DEFAULT 'user',
    PRIMARY KEY (project_id, node_id)
);

CREATE INDEX idx_membership_project ON project_membership(project_id, role);
CREATE INDEX idx_membership_node    ON project_membership(node_id);


-- ---------------------------------------------------------------------------
-- information_workspaces
-- ---------------------------------------------------------------------------
CREATE TABLE information_workspaces (
    id              TEXT PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    description_md  TEXT NOT NULL DEFAULT '',
    kind            TEXT NOT NULL DEFAULT 'mixed' CHECK (kind IN (
        'papers', 'codebase', 'notes', 'transcripts', 'web', 'mixed'
    )),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_active_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_scanned_at TEXT,
    config          TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(config))
);

CREATE INDEX idx_workspaces_slug ON information_workspaces(slug);


-- ---------------------------------------------------------------------------
-- workspace_sources
-- ---------------------------------------------------------------------------
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
CREATE TABLE workspace_membership (
    workspace_id    TEXT NOT NULL REFERENCES information_workspaces(id) ON DELETE CASCADE,
    node_id         TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    added_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (workspace_id, node_id)
);

CREATE INDEX idx_workspace_membership_workspace ON workspace_membership(workspace_id);
CREATE INDEX idx_workspace_membership_node      ON workspace_membership(node_id);


-- ---------------------------------------------------------------------------
-- project_workspaces  (M:N)
-- ---------------------------------------------------------------------------
CREATE TABLE project_workspaces (
    project_id              TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    workspace_id            TEXT NOT NULL REFERENCES information_workspaces(id) ON DELETE CASCADE,
    linked_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    role                    TEXT NOT NULL DEFAULT 'reference' CHECK (role IN (
        'primary', 'reference', 'excluded'
    )),
    weight                  REAL NOT NULL DEFAULT 1.0 CHECK (weight BETWEEN 0.0 AND 1.0),
    last_relevance_pass_at  TEXT,
    PRIMARY KEY (project_id, workspace_id)
);

CREATE INDEX idx_project_workspaces_project   ON project_workspaces(project_id);
CREATE INDEX idx_project_workspaces_workspace ON project_workspaces(workspace_id);


-- ---------------------------------------------------------------------------
-- project_effective_members  (view: workspace ∪ override ∪ pinned)
-- ---------------------------------------------------------------------------
CREATE VIEW project_effective_members AS
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
-- responses
-- ---------------------------------------------------------------------------
CREATE TABLE responses (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    session_id          TEXT NOT NULL,
    request             TEXT NOT NULL CHECK (json_valid(request)),
    output              TEXT NOT NULL,
    cited_node_ids      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(cited_node_ids)),
    -- The provenance trace (raws and the interpretations that routed to them)
    -- as JSON. One entry per raw node referenced in the answer:
    --   [{"raw_id": "01...", "interp_path": ["01A...", "01B..."]}, …]
    trace_table         TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(trace_table)),
    ts                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    client              TEXT NOT NULL DEFAULT 'unknown'
);

CREATE INDEX idx_responses_project_ts ON responses(project_id, ts);
CREATE INDEX idx_responses_session    ON responses(session_id);


-- ---------------------------------------------------------------------------
-- traces
-- ---------------------------------------------------------------------------
CREATE TABLE traces (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    session_id          TEXT NOT NULL,
    response_id         TEXT REFERENCES responses(id) ON DELETE CASCADE,
    node_id             TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    kind                TEXT NOT NULL CHECK (kind IN (
        'retrieved','cited','edited','accepted','rejected','pinned',
        'cited_kept','cited_dropped','cited_replaced',
        'requery',
        'agent_synthesised','agent_reinforced','agent_softened',
        'agent_updated_angle',
        -- Loci-of-thought traces:
        'routed_via',                 -- this interp routed retrieval to a raw
        'route_target'                -- this raw was reached via the interp path
    )),
    ts                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    client              TEXT NOT NULL DEFAULT 'unknown'
);

CREATE INDEX idx_traces_node_ts     ON traces(node_id, ts);
CREATE INDEX idx_traces_response    ON traces(response_id);
CREATE INDEX idx_traces_project_ts  ON traces(project_id, ts);
CREATE INDEX idx_traces_kind        ON traces(kind);


-- ---------------------------------------------------------------------------
-- proposals
-- ---------------------------------------------------------------------------
CREATE TABLE proposals (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind                TEXT NOT NULL CHECK (kind IN (
        'node','edge','alias','tension','broken','question'
    )),
    payload             TEXT NOT NULL CHECK (json_valid(payload)),
    status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending','accepted','dismissed'
    )),
    fingerprint         TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    resolved_at         TEXT,
    UNIQUE (project_id, fingerprint)
);

CREATE INDEX idx_proposals_project_status ON proposals(project_id, status);


-- ---------------------------------------------------------------------------
-- jobs
-- ---------------------------------------------------------------------------
CREATE TABLE jobs (
    id                  TEXT PRIMARY KEY,
    kind                TEXT NOT NULL CHECK (kind IN (
        'absorb','kickoff','reembed','reindex','export',
        'reflect','relevance','sweep_orphans','rebuild'
    )),
    project_id          TEXT REFERENCES projects(id) ON DELETE CASCADE,
    payload             TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload)),
    status              TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
        'queued','running','done','failed','cancelled'
    )),
    progress            REAL NOT NULL DEFAULT 0.0
                            CHECK (progress >= 0.0 AND progress <= 1.0),
    error               TEXT,
    result              TEXT CHECK (result IS NULL OR json_valid(result)),
    fingerprint         TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    started_at          TEXT,
    finished_at         TEXT
);

CREATE INDEX idx_jobs_status_created  ON jobs(status, created_at);
CREATE INDEX idx_jobs_project         ON jobs(project_id);
CREATE INDEX idx_jobs_fingerprint     ON jobs(fingerprint) WHERE fingerprint IS NOT NULL;


-- ---------------------------------------------------------------------------
-- agent_reflections
-- ---------------------------------------------------------------------------
CREATE TABLE agent_reflections (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    response_id     TEXT REFERENCES responses(id) ON DELETE SET NULL,
    trigger         TEXT NOT NULL CHECK (trigger IN (
        'draft','feedback','manual','kickoff',
        'link','profile_refresh','incremental','retrieve'
    )),
    instruction     TEXT NOT NULL,
    deliberation_md TEXT NOT NULL DEFAULT '',
    actions_json    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(actions_json)),
    ts              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_reflections_project_ts ON agent_reflections(project_id, ts);
CREATE INDEX idx_reflections_response   ON agent_reflections(response_id);


-- ---------------------------------------------------------------------------
-- communities
-- ---------------------------------------------------------------------------
-- Community detection over the derives_from interpretation graph (raws excluded
-- so communities are clusters of related loci).
CREATE TABLE communities (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    snapshot_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    level               INTEGER NOT NULL DEFAULT 0,
    label               TEXT,
    member_node_ids     TEXT NOT NULL CHECK (json_valid(member_node_ids))
);

CREATE INDEX idx_communities_project_snapshot ON communities(project_id, snapshot_at);


-- ---------------------------------------------------------------------------
-- nodes_fts  (FTS5 mirror)
-- ---------------------------------------------------------------------------
CREATE VIRTUAL TABLE nodes_fts USING fts5(
    node_id UNINDEXED,
    title,
    body,
    tags,
    tokenize = 'porter unicode61'
);

CREATE TRIGGER nodes_ai AFTER INSERT ON nodes BEGIN
    INSERT INTO nodes_fts(node_id, title, body, tags) VALUES (
        new.id, new.title, new.body,
        COALESCE((SELECT group_concat(tag, ' ') FROM node_tags WHERE node_id = new.id), '')
    );
END;

CREATE TRIGGER nodes_ad AFTER DELETE ON nodes BEGIN
    DELETE FROM nodes_fts WHERE node_id = old.id;
END;

CREATE TRIGGER nodes_au AFTER UPDATE OF title, body ON nodes BEGIN
    DELETE FROM nodes_fts WHERE node_id = old.id;
    INSERT INTO nodes_fts(node_id, title, body, tags) VALUES (
        new.id, new.title, new.body,
        COALESCE((SELECT group_concat(tag, ' ') FROM node_tags WHERE node_id = new.id), '')
    );
END;

CREATE TRIGGER tags_ai AFTER INSERT ON node_tags BEGIN
    DELETE FROM nodes_fts WHERE node_id = new.node_id;
    INSERT INTO nodes_fts(node_id, title, body, tags) SELECT
        n.id, n.title, n.body,
        COALESCE((SELECT group_concat(tag, ' ') FROM node_tags WHERE node_id = n.id), '')
    FROM nodes n WHERE n.id = new.node_id;
END;

CREATE TRIGGER tags_ad AFTER DELETE ON node_tags BEGIN
    DELETE FROM nodes_fts WHERE node_id = old.node_id;
    INSERT INTO nodes_fts(node_id, title, body, tags) SELECT
        n.id, n.title, n.body,
        COALESCE((SELECT group_concat(tag, ' ') FROM node_tags WHERE node_id = n.id), '')
    FROM nodes n WHERE n.id = old.node_id;
END;


-- ---------------------------------------------------------------------------
-- node_vec  (sqlite-vec ANN index)
-- ---------------------------------------------------------------------------
CREATE VIRTUAL TABLE node_vec USING vec0(
    node_id TEXT PRIMARY KEY,
    embedding FLOAT[384]
);
