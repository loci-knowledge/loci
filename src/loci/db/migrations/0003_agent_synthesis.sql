-- ============================================================================
-- 0003_agent_synthesis.sql
--
-- Pivot from the proposal queue to a silent agentic interpretation pipeline.
-- After every draft (and a few other high-signal events), an `interpreter`
-- agent autonomously reads the user's current task, the candidates retrieved,
-- and the citation-level feedback, then writes new interpretations directly
-- to the live graph at conservative confidence — no queue.
--
-- Schema changes:
--   1. Extend `traces.kind`           — citation-level signals + requery + agent.
--   2. Extend `interpretation_nodes.origin` — `agent_synthesis`.
--   3. Extend `jobs.kind`             — `reflect`.
--   4. Add `agent_reflections`        — append-only audit log of every
--                                        reflection cycle (deliberation_md is
--                                        the agent's reasoning; actions_json
--                                        is what it actually did to the graph).
--
-- SQLite CHECK constraints aren't ALTER-friendly, so we recreate the affected
-- tables with the new constraint, copying the rows. We use SQLite's
-- `legacy_alter_table=OFF` (the default since 3.26) which preserves indexes
-- and triggers automatically.
-- ============================================================================


-- ---------------------------------------------------------------------------
-- traces.kind — add citation-level + requery + agent kinds
-- ---------------------------------------------------------------------------
CREATE TABLE traces_new (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    session_id          TEXT NOT NULL,
    response_id         TEXT REFERENCES responses(id) ON DELETE CASCADE,
    node_id             TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    kind                TEXT NOT NULL CHECK (kind IN (
        -- pre-existing
        'retrieved','cited','edited','accepted','rejected','pinned',
        -- citation-level feedback (post-draft, from edited markdown)
        'cited_kept','cited_dropped','cited_replaced',
        -- session-level signals
        'requery',                       -- user re-asked similar query within window
        -- agent-driven
        'agent_synthesised',             -- this node was just created by the interpreter
        'agent_reinforced',              -- this node had its confidence/weight bumped by the interpreter
        'agent_softened'                 -- this node had its confidence reduced by the interpreter
    )),
    ts                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    client              TEXT NOT NULL DEFAULT 'unknown'
);

INSERT INTO traces_new (id, project_id, session_id, response_id, node_id, kind, ts, client)
SELECT id, project_id, session_id, response_id, node_id, kind, ts, client FROM traces;

DROP TABLE traces;
ALTER TABLE traces_new RENAME TO traces;

CREATE INDEX idx_traces_node_ts     ON traces(node_id, ts);
CREATE INDEX idx_traces_response    ON traces(response_id);
CREATE INDEX idx_traces_project_ts  ON traces(project_id, ts);
CREATE INDEX idx_traces_kind        ON traces(kind);


-- ---------------------------------------------------------------------------
-- interpretation_nodes.origin — add agent_synthesis
-- ---------------------------------------------------------------------------
CREATE TABLE interpretation_nodes_new (
    node_id                 TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    origin                  TEXT NOT NULL CHECK (origin IN (
        'user_correction','user_pin','user_summary',
        'user_explicit_create','proposal_accepted',
        'agent_synthesis'                -- written autonomously by the interpreter
    )),
    origin_session_id       TEXT,
    origin_response_id      TEXT REFERENCES responses(id) ON DELETE SET NULL
);

INSERT INTO interpretation_nodes_new (node_id, origin, origin_session_id, origin_response_id)
SELECT node_id, origin, origin_session_id, origin_response_id FROM interpretation_nodes;

DROP TABLE interpretation_nodes;
ALTER TABLE interpretation_nodes_new RENAME TO interpretation_nodes;

CREATE INDEX idx_interp_origin ON interpretation_nodes(origin);


-- ---------------------------------------------------------------------------
-- jobs.kind — add reflect
-- ---------------------------------------------------------------------------
CREATE TABLE jobs_new (
    id                  TEXT PRIMARY KEY,
    kind                TEXT NOT NULL CHECK (kind IN (
        'absorb','kickoff','reembed','reindex','export',
        'reflect'                        -- post-event interpretation synthesis
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
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    started_at          TEXT,
    finished_at         TEXT
);

INSERT INTO jobs_new (id, kind, project_id, payload, status, progress, error, result,
                      created_at, started_at, finished_at)
SELECT id, kind, project_id, payload, status, progress, error, result,
       created_at, started_at, finished_at FROM jobs;

DROP TABLE jobs;
ALTER TABLE jobs_new RENAME TO jobs;

CREATE INDEX idx_jobs_status_created ON jobs(status, created_at);
CREATE INDEX idx_jobs_project        ON jobs(project_id);


-- ---------------------------------------------------------------------------
-- agent_reflections — audit trail of every reflection cycle
-- ---------------------------------------------------------------------------
-- Each reflection cycle (one row) corresponds to one trigger event (a draft,
-- a feedback submission, an explicit /reflect call). We keep:
--   - trigger:      what fired the reflection ('draft','feedback','manual')
--   - response_id:  the response that triggered it (NULL for manual)
--   - instruction:  the user's task at the time
--   - deliberation_md: the agent's free-form reasoning before deciding
--   - actions_json: the structured list of actions actually taken
--                   [{action: create|reinforce|soften|link, ...}]
-- This is read-only audit data — never mutated after insert.
CREATE TABLE agent_reflections (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    response_id     TEXT REFERENCES responses(id) ON DELETE SET NULL,
    trigger         TEXT NOT NULL CHECK (trigger IN ('draft','feedback','manual','kickoff')),
    instruction     TEXT NOT NULL,
    deliberation_md TEXT NOT NULL DEFAULT '',
    actions_json    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(actions_json)),
    ts              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_reflections_project_ts ON agent_reflections(project_id, ts);
CREATE INDEX idx_reflections_response   ON agent_reflections(response_id);
