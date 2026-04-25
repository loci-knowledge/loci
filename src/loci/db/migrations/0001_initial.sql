-- ============================================================================
-- 0001_initial.sql
--
-- Schema for the loci graph store. See PLAN.md §Data model for the design.
--
-- Layout:
--   nodes                   base shape, discriminated by `kind` (raw|interpretation)
--   raw_nodes               kind-specific columns for raw sources
--   interpretation_nodes    kind-specific columns for user interpretations
--   node_tags               many-to-many tag join
--   edges                   typed graph edges (interp↔interp; cites: interp→raw)
--   projects                project metadata + profile
--   project_membership      which nodes belong to which project (with role)
--   responses               record of every retrieve/draft call
--   traces                  per-node provenance for each response
--   proposals               proposed nodes/edges awaiting accept/dismiss
--   jobs                    background-job queue (absorb, contradiction, etc.)
--   communities             absorb-time community detection snapshots
--   nodes_fts               FTS5 mirror of (title, body, tags) for BM25
--   node_vec                sqlite-vec ANN index keyed by node_id
--
-- Conventions:
--   - All ids are ULIDs (26-char base32, time-sortable).
--   - All timestamps are ISO-8601 UTC strings: 'YYYY-MM-DDTHH:MM:SS.fffZ'.
--     SQLite's `strftime('%Y-%m-%dT%H:%M:%fZ', 'now')` produces them.
--   - JSON columns store JSON text validated by SQLite's json() function.
-- ============================================================================


-- ---------------------------------------------------------------------------
-- nodes (base table)
-- ---------------------------------------------------------------------------
CREATE TABLE nodes (
    id                  TEXT PRIMARY KEY,                   -- ULID
    kind                TEXT NOT NULL CHECK (kind IN ('raw','interpretation')),
    -- subkind constraint depends on kind; checked here as a single union for
    -- simplicity. Application layer is responsible for kind/subkind coherence.
    subkind             TEXT NOT NULL CHECK (subkind IN (
        -- raw subkinds
        'pdf','md','code','html','transcript','txt','image',
        -- interpretation subkinds (PLAN.md §Inspiration carried forward)
        'philosophy','pattern','tension','decision','question',
        'touchstone','experiment','metaphor'
    )),
    title               TEXT NOT NULL,
    body                TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_accessed_at    TEXT,
    access_count        INTEGER NOT NULL DEFAULT 0,
    -- confidence ∈ [0,1]; only meaningful for kind='interpretation' but stored
    -- on the base table to keep one read path.
    confidence          REAL NOT NULL DEFAULT 1.0
                            CHECK (confidence >= 0.0 AND confidence <= 1.0),
    -- status state machine (PLAN.md §Data model):
    --   proposed → live → dirty → stale → dismissed
    -- Raw nodes are always 'live' (their existence is the assertion);
    -- interpretation nodes traverse the full machine.
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
    -- sha256 of the *content*, hex-encoded, truncated to 16 chars (64 bits).
    -- Collisions for personal-scale corpora are vanishingly unlikely; we use
    -- the truncation only because SQLite indexes on shorter strings are
    -- materially smaller. Content is stored on disk under blob_dir keyed by
    -- the FULL hash; we keep the full hash in `body` for raw_nodes is wrong
    -- — instead we store the truncated hash here and reconstruct the full
    -- path via a lookup. (See loci.ingest.content_hash.)
    content_hash        TEXT NOT NULL UNIQUE,
    canonical_path      TEXT NOT NULL,
    mime                TEXT NOT NULL,
    size_bytes          INTEGER NOT NULL CHECK (size_bytes >= 0),
    -- false if the file at canonical_path is missing/deleted (audit pass).
    -- Interpretations that `cites` a non-source-of-truth raw lose effective
    -- support and may transition to 'stale' — see PLAN.md §Edge cases.
    source_of_truth     INTEGER NOT NULL DEFAULT 1 CHECK (source_of_truth IN (0,1))
);

CREATE INDEX idx_raw_canonical_path ON raw_nodes(canonical_path);


-- ---------------------------------------------------------------------------
-- interpretation_nodes  (kind = 'interpretation')
-- ---------------------------------------------------------------------------
CREATE TABLE interpretation_nodes (
    node_id                 TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    -- How this interpretation came into existence. Drives different
    -- confidence priors and absorb-time treatment.
    origin                  TEXT NOT NULL CHECK (origin IN (
        'user_correction','user_pin','user_summary',
        'user_explicit_create','proposal_accepted'
    )),
    origin_session_id       TEXT,
    origin_response_id      TEXT REFERENCES responses(id) ON DELETE SET NULL
);

CREATE INDEX idx_interp_origin ON interpretation_nodes(origin);


-- ---------------------------------------------------------------------------
-- node_tags
-- ---------------------------------------------------------------------------
-- Tags are user-supplied free-form labels. Stored separately from nodes so
-- we can query "all nodes tagged X" cheaply and so the tag set itself is
-- introspectable.
CREATE TABLE node_tags (
    node_id     TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    tag         TEXT NOT NULL,
    PRIMARY KEY (node_id, tag)
);

CREATE INDEX idx_node_tags_tag ON node_tags(tag);


-- ---------------------------------------------------------------------------
-- edges
-- ---------------------------------------------------------------------------
-- Typed directed edges in the interpretation graph. Symmetric edge types
-- (reinforces, contradicts, aliases, co_occurs) store both rows so all
-- queries are simple `WHERE src=?` joins; the application layer enforces the
-- reciprocal write in the same transaction. `specializes` inserts also write
-- a reciprocal `generalizes` (its inverse).
--
-- src kind constraints (PLAN.md §Edges):
--   cites:        src=interpretation, dst=raw
--   reinforces|contradicts|extends|specializes|aliases|co_occurs:
--                 src=interpretation, dst=interpretation
-- These are enforced in the application repository, not as CHECK constraints,
-- because SQLite can't reach across tables in CHECK.
CREATE TABLE edges (
    id                  TEXT PRIMARY KEY,
    src                 TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    dst                 TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    type                TEXT NOT NULL CHECK (type IN (
        'cites','reinforces','contradicts','extends',
        'specializes','generalizes','aliases','co_occurs'
    )),
    weight              REAL NOT NULL DEFAULT 1.0
                            CHECK (weight >= 0.0 AND weight <= 1.0),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_by          TEXT NOT NULL DEFAULT 'user' CHECK (created_by IN (
        'user','system','proposal_accepted'
    )),
    -- True for the symmetric edge types. Stored alongside `type` for direct
    -- query (no need to re-derive). Not a constraint — informational.
    symmetric           INTEGER NOT NULL DEFAULT 0 CHECK (symmetric IN (0,1)),
    UNIQUE (src, dst, type)
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
    -- JSON config: absorb cadence, retrieval defaults, etc. Validated by app.
    config              TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(config))
);


-- ---------------------------------------------------------------------------
-- project_membership
-- ---------------------------------------------------------------------------
-- A project is a *view* over the global graph. Membership asserts inclusion.
-- One node can participate in many projects without duplication.
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
-- responses
-- ---------------------------------------------------------------------------
-- Every retrieve/draft call writes a row here. `cited_node_ids` is a JSON
-- array; the join into `traces` is the canonical per-node record.
CREATE TABLE responses (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    session_id          TEXT NOT NULL,
    -- The original request payload (jsonb-equivalent in SQLite).
    request             TEXT NOT NULL CHECK (json_valid(request)),
    output              TEXT NOT NULL,
    cited_node_ids      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(cited_node_ids)),
    ts                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    client              TEXT NOT NULL DEFAULT 'unknown'
);

CREATE INDEX idx_responses_project_ts ON responses(project_id, ts);
CREATE INDEX idx_responses_session    ON responses(session_id);


-- ---------------------------------------------------------------------------
-- traces
-- ---------------------------------------------------------------------------
-- The episodic provenance log (PLAN.md §Inspiration: episodic-vs-semantic).
-- One row per (response, node, kind). Replayed at absorb to update
-- access_count / last_accessed_at / confidence.
CREATE TABLE traces (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    session_id          TEXT NOT NULL,
    response_id         TEXT REFERENCES responses(id) ON DELETE CASCADE,
    node_id             TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    kind                TEXT NOT NULL CHECK (kind IN (
        'retrieved','cited','edited','accepted','rejected','pinned'
    )),
    ts                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    client              TEXT NOT NULL DEFAULT 'unknown'
);

CREATE INDEX idx_traces_node_ts     ON traces(node_id, ts);
CREATE INDEX idx_traces_response    ON traces(response_id);
CREATE INDEX idx_traces_project_ts  ON traces(project_id, ts);


-- ---------------------------------------------------------------------------
-- proposals
-- ---------------------------------------------------------------------------
-- Items awaiting user accept/dismiss. Generated by absorb (alias detection,
-- contradiction detection, broken-support tensions, kickoff questions).
CREATE TABLE proposals (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    -- What's being proposed. Drives the payload schema:
    --   node       — payload = {subkind, title, body, tags, supports?, related?}
    --   edge       — payload = {src, dst, type, weight}
    --   alias      — payload = {a, b, similarity}              (specialised case of edge)
    --   tension    — payload = {about_node_id, reason, evidence_node_ids}
    --   broken     — payload = {about_node_id, missing_raw_id}
    kind                TEXT NOT NULL CHECK (kind IN (
        'node','edge','alias','tension','broken','question'
    )),
    payload             TEXT NOT NULL CHECK (json_valid(payload)),
    -- 'pending' until the user acts. Dismissed proposals are kept so we don't
    -- re-propose the same body (PLAN.md §Interaction vocabulary).
    status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending','accepted','dismissed'
    )),
    -- A short content fingerprint used to dedupe re-proposals. Computed in
    -- the app layer (e.g. sha256 of canonicalised payload) so we don't need
    -- to recompute in SQL.
    fingerprint         TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    resolved_at         TEXT,
    UNIQUE (project_id, fingerprint)
);

CREATE INDEX idx_proposals_project_status ON proposals(project_id, status);


-- ---------------------------------------------------------------------------
-- jobs
-- ---------------------------------------------------------------------------
-- SQLite-backed job queue. We don't pull in Redis/arq for a local server —
-- one writer (the worker), many readers (status polls), all in-process.
-- Workers claim with `UPDATE ... WHERE status='queued' RETURNING` (atomic
-- via the SQLite UPDATE...RETURNING extension since 3.35).
CREATE TABLE jobs (
    id                  TEXT PRIMARY KEY,
    kind                TEXT NOT NULL CHECK (kind IN (
        'absorb','kickoff','reembed','reindex','export'
    )),
    project_id          TEXT REFERENCES projects(id) ON DELETE CASCADE,
    payload             TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload)),
    status              TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
        'queued','running','done','failed','cancelled'
    )),
    progress            REAL NOT NULL DEFAULT 0.0
                            CHECK (progress >= 0.0 AND progress <= 1.0),
    error               TEXT,
    -- result: JSON written by the job on success. Free-form per `kind`.
    result              TEXT CHECK (result IS NULL OR json_valid(result)),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    started_at          TEXT,
    finished_at         TEXT
);

CREATE INDEX idx_jobs_status_created ON jobs(status, created_at);
CREATE INDEX idx_jobs_project        ON jobs(project_id);


-- ---------------------------------------------------------------------------
-- communities
-- ---------------------------------------------------------------------------
-- Snapshots from the absorb-time community detection pass (Leiden over the
-- co_occurs + reinforces graph). Each row is one community in one snapshot.
-- Older snapshots are kept for diffing; the latest per project is the live one.
CREATE TABLE communities (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    snapshot_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    level               INTEGER NOT NULL DEFAULT 0,   -- hierarchical level (0 = leaves)
    label               TEXT,                          -- LLM-derived label, optional
    -- JSON array of node ids that belong to this community.
    member_node_ids     TEXT NOT NULL CHECK (json_valid(member_node_ids))
);

CREATE INDEX idx_communities_project_snapshot ON communities(project_id, snapshot_at);


-- ---------------------------------------------------------------------------
-- nodes_fts  (FTS5 mirror)
-- ---------------------------------------------------------------------------
-- External-content FTS5 mirroring (title, body, tags). We use external
-- content so we don't double-store the text; updates go through triggers.
-- Tags are joined as a single space-separated string (per FTS5 idiom for
-- multi-valued columns).
--
-- The `content_rowid` is the row in `nodes` (we map node.id → an integer
-- rowid via a small mapping table to avoid scanning huge TEXT primary keys).
-- Actually FTS5 requires an integer content_rowid; since `nodes.id` is TEXT
-- (ULID), we use FTS5's *contentless* table mode and re-fetch the row by
-- `node_id` after the MATCH. This is the standard pattern when the source
-- table has a non-integer PK.
CREATE VIRTUAL TABLE nodes_fts USING fts5(
    node_id UNINDEXED,
    title,
    body,
    tags,
    tokenize = 'porter unicode61'
);

-- Triggers to keep nodes_fts in sync. Tags are pulled inline from node_tags.
-- We delete-then-insert on UPDATE to keep the trigger logic simple; FTS5
-- doesn't support partial updates anyway.
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

-- Tag changes also need to propagate. We rebuild the FTS row's `tags` column.
-- The simplest correct path is to re-INSERT (with delete) — same as nodes_au.
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
-- Embedding dim is fixed at table creation. Default is 384 (BAAI/bge-small-en-v1.5).
-- Swapping to a different model requires a new migration that creates a new
-- vec table (e.g. `node_vec_768`) and a one-shot reembed job.
--
-- vec0 stores the embedding as FLOAT[N]. The PK is `node_id` (TEXT) — vec0
-- supports auxiliary text PK columns since 0.1.6.
CREATE VIRTUAL TABLE node_vec USING vec0(
    node_id TEXT PRIMARY KEY,
    embedding FLOAT[384]
);
