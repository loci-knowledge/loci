-- Migration 0003: add 'autoresearch' to jobs.kind CHECK constraint.
--
-- SQLite does not support ALTER TABLE ... DROP/ADD CONSTRAINT, so we recreate
-- the jobs table with the updated constraint, copy all rows, and swap names.
-- Indexes are also recreated; the FK reference from jobs.project_id is preserved.

PRAGMA foreign_keys = OFF;

CREATE TABLE jobs_new (
    id                  TEXT PRIMARY KEY,
    kind                TEXT NOT NULL CHECK (kind IN (
        'absorb','kickoff','reembed','reindex','export',
        'reflect','relevance','sweep_orphans','rebuild',
        'autoresearch'
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

INSERT INTO jobs_new SELECT * FROM jobs;

DROP TABLE jobs;
ALTER TABLE jobs_new RENAME TO jobs;

CREATE INDEX idx_jobs_status_created  ON jobs(status, created_at);
CREATE INDEX idx_jobs_project         ON jobs(project_id);
CREATE INDEX idx_jobs_fingerprint     ON jobs(fingerprint) WHERE fingerprint IS NOT NULL;

PRAGMA foreign_keys = ON;
