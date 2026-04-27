-- ---------------------------------------------------------------------------
-- node_revisions  (event-sourced edit log for interpretation nodes)
-- ---------------------------------------------------------------------------
-- Every create / edit / delete on an interpretation node appends a row here.
-- The materialized slot values live in interpretation_nodes as before; this
-- table records what *changed* and what the prior state was, so users and
-- agents can review history, undo, and build personalization training data.
--
-- op values:
--   create        new interpretation node inserted
--   update_locus  one or more of relation_md/overlap_md/source_anchor_md/angle changed
--   update_body   title or body changed
--   set_angle     angle (only) changed
--   hard_delete   node deleted (prior_values carries full slot + edge snapshot)
--   revert        a prior revision was re-applied (prior_values = current before revert)
CREATE TABLE node_revisions (
    id                  TEXT PRIMARY KEY,
    node_id             TEXT NOT NULL,
    ts                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    actor               TEXT NOT NULL DEFAULT 'system'
                            CHECK (actor IN ('user','agent','system')),
    source_tool         TEXT,
    op                  TEXT NOT NULL CHECK (op IN (
                            'create','update_locus','update_body','set_angle',
                            'hard_delete','revert'
                        )),
    reason              TEXT,
    prior_values        TEXT NOT NULL CHECK (json_valid(prior_values)),
    new_values          TEXT NOT NULL CHECK (json_valid(new_values)),
    parent_revision_id  TEXT REFERENCES node_revisions(id) ON DELETE SET NULL
    -- node_id intentionally has no FK — we want revisions to survive even after
    -- hard_delete (the hard_delete row IS the tombstone). Cascade-on-delete would
    -- defeat the audit trail.
);

CREATE INDEX idx_node_revisions_node_ts ON node_revisions(node_id, ts);
CREATE INDEX idx_node_revisions_op      ON node_revisions(op);


-- ---------------------------------------------------------------------------
-- preference_pairs  (training corpus for future CrossEncoder reranker)
-- ---------------------------------------------------------------------------
-- Populated by jobs/preference_pairs.py after every successful draft.
-- Each row is one (positive, negative) pair derived from cited_kept/dropped
-- traces, ready to use as a sentence-transformers CrossEncoder training example.
CREATE TABLE preference_pairs (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    response_id     TEXT NOT NULL REFERENCES responses(id) ON DELETE CASCADE,
    query           TEXT NOT NULL,
    positive_node   TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    negative_node   TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    signal          TEXT NOT NULL CHECK (signal IN (
                        'cited_kept_vs_dropped',
                        'cited_kept_vs_replaced',
                        'pinned_vs_unranked'
                    )),
    ts              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX idx_pref_pairs_project_ts ON preference_pairs(project_id, ts);
CREATE INDEX idx_pref_pairs_response   ON preference_pairs(response_id);
