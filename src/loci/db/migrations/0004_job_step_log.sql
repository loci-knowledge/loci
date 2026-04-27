-- Migration 0004: add step_log column to jobs for streaming intermediate research steps.
-- Stores a JSON array of {t, tool, msg} objects written by the research agent
-- as it calls paper-discovery tools. Polled via loci_research_status.
ALTER TABLE jobs ADD COLUMN step_log TEXT DEFAULT NULL;
