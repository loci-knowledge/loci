-- ============================================================================
-- 0002_chunks.sql — span-level retrieval for RawNodes
--
-- Motivation: 0001 stored one embedding per file. Citing a 50-page PDF as
-- "[C1] supports the claim" gives the LLM a name without the relevant span,
-- which is the dominant hallucination failure mode in graph-RAG. KG2RAG
-- (Zhu et al., 2025) and related work all retrieve at chunk granularity.
--
-- Design:
--   - RawNode identity is preserved. One node per file (content-addressed).
--   - A new `raw_chunks` table holds spans. One row per chunk, ordered by
--     `ord` within the parent raw.
--   - `chunks_fts` mirrors chunk text into FTS5 for BM25-on-chunks.
--   - `chunk_vec` is the sqlite-vec ANN index for chunk embeddings.
--   - Existing `node_vec` continues to hold interpretation embeddings.
--     For raws that have chunks, `node_vec` is no longer the primary path.
--
-- Backfill: existing raws have no chunks until re-scanned (or `loci backfill
-- chunks` is run). The retrieval layer falls back to `node_vec` for raws
-- without chunk rows so we don't break older databases.
-- ============================================================================


CREATE TABLE raw_chunks (
    id              TEXT PRIMARY KEY,
    raw_id          TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    -- 0-based ordinal within the parent raw. Used to reassemble + render
    -- adjacent context.
    ord             INTEGER NOT NULL,
    -- Byte offsets into the parent raw's body (the extracted text the
    -- chunker received). char_start <= char_end <= len(body).
    char_start      INTEGER NOT NULL CHECK (char_start >= 0),
    char_end        INTEGER NOT NULL CHECK (char_end >= char_start),
    -- The chunk text itself. Embedded + FTS-indexed via the triggers below.
    text            TEXT NOT NULL,
    -- Optional section/heading hint (e.g. "## Methods"). NULL for code or
    -- structureless text.
    section         TEXT,
    UNIQUE (raw_id, ord)
);

CREATE INDEX idx_raw_chunks_raw ON raw_chunks(raw_id, ord);


CREATE VIRTUAL TABLE chunks_fts USING fts5(
    chunk_id UNINDEXED,
    raw_id UNINDEXED,
    text,
    section,
    tokenize = 'porter unicode61'
);


CREATE TRIGGER raw_chunks_ai AFTER INSERT ON raw_chunks BEGIN
    INSERT INTO chunks_fts(chunk_id, raw_id, text, section)
    VALUES (new.id, new.raw_id, new.text, COALESCE(new.section, ''));
END;

CREATE TRIGGER raw_chunks_ad AFTER DELETE ON raw_chunks BEGIN
    DELETE FROM chunks_fts WHERE chunk_id = old.id;
    -- chunk_vec is a vec0 virtual table; FK cascade does not reach it.
    DELETE FROM chunk_vec WHERE chunk_id = old.id;
END;

CREATE TRIGGER raw_chunks_au AFTER UPDATE OF text, section ON raw_chunks BEGIN
    DELETE FROM chunks_fts WHERE chunk_id = old.id;
    INSERT INTO chunks_fts(chunk_id, raw_id, text, section)
    VALUES (new.id, new.raw_id, new.text, COALESCE(new.section, ''));
END;


CREATE VIRTUAL TABLE chunk_vec USING vec0(
    chunk_id TEXT PRIMARY KEY,
    embedding FLOAT[384]
);
