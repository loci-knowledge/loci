-- ============================================================================
-- 0006_jobs_kinds.sql
--
-- Extends jobs.kind to include:
--   relevance     — focused relevance-synthesis pass for a project↔workspace pair
--   sweep_orphans — marks interpretation nodes dirty when their evidence raws
--                   leave a project's effective membership (e.g., workspace unlinked)
--
-- Also adds jobs.fingerprint for enqueue-side deduplication: prevents relevance-job
-- storms when a workspace receives a burst of new files. The app layer computes a
-- fingerprint (e.g., sha256 of (project_id, workspace_id, scope)) before inserting;
-- if a queued/running job with the same fingerprint exists, it skips the enqueue.
-- ============================================================================

CREATE TABLE jobs_new (
    id                  TEXT PRIMARY KEY,
    kind                TEXT NOT NULL CHECK (kind IN (
        'absorb', 'kickoff', 'reembed', 'reindex', 'export',
        'reflect',
        'relevance',       -- project↔workspace relevance-synthesis pass
        'sweep_orphans'    -- mark dirty interps whose evidence raws left the project
    )),
    project_id          TEXT REFERENCES projects(id) ON DELETE CASCADE,
    payload             TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload)),
    status              TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
        'queued', 'running', 'done', 'failed', 'cancelled'
    )),
    progress            REAL NOT NULL DEFAULT 0.0
                            CHECK (progress >= 0.0 AND progress <= 1.0),
    error               TEXT,
    result              TEXT CHECK (result IS NULL OR json_valid(result)),
    -- Content fingerprint for enqueue-side deduplication. The app layer
    -- computes and checks this before INSERT to avoid duplicate jobs.
    fingerprint         TEXT,
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

CREATE INDEX idx_jobs_status_created  ON jobs(status, created_at);
CREATE INDEX idx_jobs_project         ON jobs(project_id);
CREATE INDEX idx_jobs_fingerprint     ON jobs(fingerprint) WHERE fingerprint IS NOT NULL;


-- Extend agent_reflections.trigger to include the new job triggers.
CREATE TABLE agent_reflections_new (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    response_id     TEXT REFERENCES responses(id) ON DELETE SET NULL,
    trigger         TEXT NOT NULL CHECK (trigger IN (
        'draft', 'feedback', 'manual', 'kickoff',
        'link',             -- workspace linked to project
        'profile_refresh',  -- project profile changed
        'incremental'       -- new raws arrived in a linked workspace
    )),
    instruction     TEXT NOT NULL,
    deliberation_md TEXT NOT NULL DEFAULT '',
    actions_json    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(actions_json)),
    ts              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

INSERT INTO agent_reflections_new
SELECT * FROM agent_reflections;

DROP TABLE agent_reflections;
ALTER TABLE agent_reflections_new RENAME TO agent_reflections;

CREATE INDEX idx_reflections_project_ts ON agent_reflections(project_id, ts);
CREATE INDEX idx_reflections_response   ON agent_reflections(response_id);
