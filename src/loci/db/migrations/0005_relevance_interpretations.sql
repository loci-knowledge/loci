-- ============================================================================
-- 0005_relevance_interpretations.sql
--
-- Extends the interpretation layer to support project↔information relevance.
--
-- Changes:
--   1. nodes.subkind          — add 'relevance' (via writable_schema patch).
--   2. interpretation_nodes   — add angle, rationale_md columns.
--   3. edges                  — add rationale, angle columns (per-cite rationale).
--
-- A 'relevance' interpretation expresses WHY one or more information sources
-- matter to a project: it cites ≥2 raws (often from different workspaces) and
-- carries an angle (closed vocab: applicable_pattern, experimental_setup, …)
-- and a rationale_md ("because-clause"). Per-edge rationale differentiates each
-- cited raw's individual contribution.
--
-- nodes.subkind CHECK:
--   The standard SQLite "12-step rename" cannot be used here because several
--   tables outside nodes (raw_nodes, interpretation_nodes, edges, …) have FK
--   references to nodes(id), AND node_tags has triggers (tags_ai, tags_ad) that
--   reference nodes by name. Dropping nodes would leave those triggers pointing
--   at a non-existent table during the gap, which SQLite rejects.
--
--   Instead we patch sqlite_schema directly via PRAGMA writable_schema=ON.
--   This is the recommended approach in the SQLite docs for constraint-only
--   schema edits. We run integrity_check afterwards to verify the patch is
--   sound. The Python Pydantic model is the primary guard; the SQL CHECK is
--   belt-and-suspenders and is kept in sync here as a secondary guard.
-- ============================================================================


-- ---------------------------------------------------------------------------
-- 1. Extend nodes.subkind CHECK to include 'relevance'
-- ---------------------------------------------------------------------------
PRAGMA writable_schema=ON;

UPDATE sqlite_schema
SET    sql = replace(
           sql,
           '''metaphor''',
           '''metaphor'', ''relevance'''
       )
WHERE  type = 'table'
  AND  name = 'nodes';

PRAGMA writable_schema=OFF;

-- Verify the schema patch is internally consistent.
-- If this fails, the migration is rolled back by the runner.
PRAGMA integrity_check;


-- ---------------------------------------------------------------------------
-- 2. Add angle and rationale_md to interpretation_nodes
-- ---------------------------------------------------------------------------
-- angle: closed vocabulary of relevance angles. NULL for non-relevance interps.
-- rationale_md: the "because-clause" — why these sources matter to the project.
ALTER TABLE interpretation_nodes ADD COLUMN angle TEXT;
ALTER TABLE interpretation_nodes ADD COLUMN rationale_md TEXT NOT NULL DEFAULT '';


-- ---------------------------------------------------------------------------
-- 3. Add rationale and angle to edges
-- ---------------------------------------------------------------------------
-- Per-citation rationale: within a single relevance interpretation that cites
-- multiple raws, each cites edge can carry its own sub-rationale explaining
-- that specific raw's contribution. angle mirrors the interpretation-level angle
-- for denormalised access in edge queries.
ALTER TABLE edges ADD COLUMN rationale TEXT;
ALTER TABLE edges ADD COLUMN angle TEXT;
