-- ============================================================================
-- 0007_schema_simplification.sql
--
-- Simplifies the graph vocabulary to focus on high-signal interpretation types.
--
-- New interpretation subkinds: relevance, decision, philosophy, tension
-- New edge types: cites (interp→raw), semantic (interp↔interp), actual (raw↔raw)
--
-- Note: the CHECK constraint extensions (adding 'semantic','actual' to edges
-- and 'relevance' to nodes) were applied to the schema via an earlier partial
-- run. This migration handles the data migrations only (idempotent no-ops if
-- already applied).
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. Extend CHECK constraints if not already extended (safe to re-run)
-- ---------------------------------------------------------------------------
PRAGMA writable_schema=ON;

UPDATE sqlite_schema
SET sql = replace(sql, '''co_occurs''', '''co_occurs'',''semantic'',''actual''')
WHERE type = 'table' AND name = 'edges'
  AND sql LIKE '%co_occurs%'
  AND sql NOT LIKE '%semantic%';

UPDATE sqlite_schema
SET sql = replace(sql, '''metaphor'')',
                       '''metaphor'',''relevance'',''tension'',''decision'',''philosophy'')')
WHERE type = 'table' AND name = 'nodes'
  AND sql LIKE '%metaphor%'
  AND sql NOT LIKE '%relevance%';

PRAGMA writable_schema=OFF;

-- ---------------------------------------------------------------------------
-- 2. Migrate interpretation subkind data to the 4 canonical values
-- ---------------------------------------------------------------------------
UPDATE nodes SET subkind = 'tension'
WHERE kind = 'interpretation' AND subkind = 'question';

UPDATE nodes SET subkind = 'decision'
WHERE kind = 'interpretation' AND subkind IN ('pattern', 'experiment');

UPDATE nodes SET subkind = 'philosophy'
WHERE kind = 'interpretation' AND subkind IN ('touchstone', 'metaphor');

-- ---------------------------------------------------------------------------
-- 3. Migrate edge type data (interp↔interp edges → 'semantic')
--    Deduplicate first: when multiple old-type edges exist between the same
--    pair, keep the one with the highest weight and delete the rest.
-- ---------------------------------------------------------------------------
DELETE FROM edges
WHERE type IN ('co_occurs','reinforces','contradicts','extends',
               'specializes','generalizes','aliases')
  AND id NOT IN (
      SELECT id FROM (
          SELECT id, ROW_NUMBER() OVER (PARTITION BY src, dst ORDER BY weight DESC, created_at ASC) AS rn
          FROM edges
          WHERE type IN ('co_occurs','reinforces','contradicts','extends',
                         'specializes','generalizes','aliases')
      ) WHERE rn = 1
  );

UPDATE edges SET type = 'semantic'
WHERE type IN ('co_occurs','reinforces','contradicts','extends',
               'specializes','generalizes','aliases');

PRAGMA integrity_check;
