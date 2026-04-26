-- Migration 0008: Add 'retrieve' to agent_reflections.trigger CHECK constraint.
--
-- The loci_retrieve MCP tool now enqueues a lightweight reflect job after
-- each retrieval call; those jobs need 'retrieve' as a valid trigger value.

CREATE TABLE agent_reflections_new2 (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    response_id     TEXT REFERENCES responses(id) ON DELETE SET NULL,
    trigger         TEXT NOT NULL CHECK (trigger IN (
        'draft', 'feedback', 'manual', 'kickoff',
        'link',             -- workspace linked to project
        'profile_refresh',  -- project profile changed
        'incremental',      -- new raws arrived in a linked workspace
        'retrieve'          -- lightweight reflect after loci_retrieve
    )),
    instruction     TEXT NOT NULL,
    deliberation_md TEXT NOT NULL DEFAULT '',
    actions_json    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(actions_json)),
    ts              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

INSERT INTO agent_reflections_new2
SELECT * FROM agent_reflections;

DROP TABLE agent_reflections;
ALTER TABLE agent_reflections_new2 RENAME TO agent_reflections;

CREATE INDEX idx_reflections_project_ts ON agent_reflections(project_id, ts);
CREATE INDEX idx_reflections_response   ON agent_reflections(response_id);
