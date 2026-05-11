# Contributing to loci

## Development setup

```bash
git clone https://github.com/loci-knowledge/loci
cd loci
uv sync --extra dev
cp .env.example .env   # add OPENAI_API_KEY or OPENROUTER_API_KEY
uv run loci server     # verify it starts
```

Lint + test before pushing:

```bash
uv run ruff check src/ tests/
uv run pytest -m "not slow and not integration" --tb=short
```

## Architecture overview

loci is organized by concern. Each subpackage owns one thing end-to-end:

```
src/loci/
  ui/         CLI (cyclopts) + TUI wizard
  api/        FastAPI app + REST routes
  mcp/        FastMCP server, project resolution, session hash, event recording
  graph/      sources, aspects, concept_edges, interpretations, projects, workspaces
  retrieve/   BM25 + ANN + HyDE + concept expansion + pipeline
  capture/    URL/file/text ingest + folder/aspect suggestion + link parsing
  ingest/     walker, hash, extractors, chunker, chunks repo, pipeline
  jobs/       queue + worker + handlers
  embed/      sentence-transformers wrapper
  llm/        pydantic-ai wrapper (Anthropic / OpenAI / OpenRouter)
  db/         schema.sql + incremental migrations
  config.py   Settings + ~/.loci/ paths
```

### Data flow

```
ingest (capture/) → jobs/ → graph/ → retrieve/
   ↑                                      ↓
mcp/ ←──────────────────────────────── mcp/
```

1. **Ingest** (`capture/ingest.py`): fetch → extract → chunk → embed → write
   `nodes`, `raw_nodes`, `raw_chunks`, `chunk_vec`.
2. **Jobs** (`jobs/`): background worker drains a SQLite queue:
   - `classify_aspects` — global aspect tagging; cascades to `infer_interpretation`
   - `infer_interpretation` — per-project LLM interpretation (daily-bucketed)
   - `refresh_project_edges` — cheap co-aspect edge recompute after user edits
   - `sweep_interpretations` — periodic staleness enqueuer
   - `parse_links` — wikilinks + citations → `concept_edges`
   - `embed_missing` — re-embed nodes with no vector
3. **Graph** (`graph/`): repositories over SQLite tables — `AspectRepository`,
   `ConceptEdgeRepository`, `ProjectInterpretationRepository`, etc.
4. **Retrieve** (`retrieve/pipeline.py`): query → expand aspects → HyDE → BM25 +
   ANN → RRF → graph rerank → build "why surfaced" with project interpretation.
5. **MCP** (`mcp/server.py`): six tools, three resource templates, completion
   handlers. All tool calls fire `record_event()` which writes usage rows and
   enqueues interpretation jobs.

### Aspect layer: two tables, one purpose

| table | scope | written by | read by |
|---|---|---|---|
| `resource_aspects` | global | `classify_aspects` job | fallback when no project |
| `project_resource_aspects` | per-project | `infer_interpretation` job, user via MCP | retrieval, `loci_aspects` |

User-set aspects (`source='user'`) are *gold*: `ON CONFLICT ... WHERE source != 'user'`
prevents LLM inference from overwriting them.

### Interpretation freshness

`project_interpretations.inputs_hash` = SHA-256 of
`(content_hash, profile_md, top_aspects, recent_queries)`. If unchanged, the job
exits without calling the LLM. This prevents redundant API calls when nothing
material has changed.

Daily bucket fingerprint:
`infer_interpretation:{project_id}:{resource_id}:{YYYY-MM-DD}`

Multiple triggers in a day (recall, browse, conversation hook) all collapse to
one LLM call.

## Schema and migrations

The schema is in `src/loci/db/schema.sql` — applied via `init_schema()`.
`init_schema()` runs **migrations first** (so existing databases gain new columns
before the full schema script tries to create indexes on those columns), then
runs `executescript(schema.sql)` (all `CREATE … IF NOT EXISTS`, idempotent).

To add a schema change:

1. Update `schema.sql` to reflect the target state (for fresh installs).
2. Add a migration function to `src/loci/db/migrations.py` that makes the same
   change to existing databases. Every migration must:
   - Be idempotent (safe to re-run).
   - Guard against missing tables (early-return if the table doesn't exist yet —
     fresh installs run migrations before `executescript` creates the tables).
   - Append to `_MIGRATIONS` with an incrementing version number — **never reorder
     or delete entries**.

Example:

```python
def _m002_add_foo_bar(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "some_table" not in tables:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(some_table)")}
    if "new_col" not in cols:
        conn.execute("ALTER TABLE some_table ADD COLUMN new_col TEXT")

_MIGRATIONS = [
    (1, _m001_aspects_v2),
    (2, _m002_add_foo_bar),   # ← append; never reorder
]
```

## Adding a new job kind

1. Create `src/loci/jobs/my_job.py` with `async def handle_my_job(job, conn, settings) -> dict`.
2. Register it in `src/loci/jobs/worker.py::_handlers()`.
3. The `jobs.kind` column has no CHECK constraint — no schema migration needed.
4. Enqueue via `from loci.jobs.queue import enqueue; enqueue(conn, kind="my_job", ...)`.

## Adding a new MCP tool

1. Add the tool function inside `build_mcp_server()` in `mcp/server.py`.
2. Call `record_event()` from `mcp/events.py` with the appropriate `tool=` name.
3. If the tool surfaces resources, `enqueue_inference=True` fires the daily-bucketed
   interpretation pipeline.
4. Add to the MCP surface table in `README.md` and `CLAUDE.md`.

## Code style

- Formatter / linter: `ruff` (config in `pyproject.toml`)
- Type hints on all public functions
- No comments unless the *why* is non-obvious
- `from __future__ import annotations` in every module
- Lazy imports inside functions when they pull in heavy dependencies (torch, etc.)

## Dependency changes

Runtime deps in `[project.dependencies]`; dev-only in
`[project.optional-dependencies] dev`. Use version ranges, not exact pins.

After editing `pyproject.toml`:

```bash
uv lock && git add uv.lock
```

## Submitting changes

1. Fork and branch from `main`.
2. Keep commits focused.
3. Open a PR against `main` — CI runs on Ubuntu / macOS × Python 3.12 / 3.13.

## Release process

Releases fire on `v*` tags:

```bash
# Bump version in pyproject.toml, commit, then:
git tag v0.2.0
git push origin v0.2.0
```

The `release.yml` workflow builds and publishes to PyPI via trusted publishing.

### Version policy

- **patch** (`0.1.x`) — bug fixes, doc updates, dependency bumps
- **minor** (`0.x.0`) — new features, additive API/schema changes
- **major** (`x.0.0`) — breaking CLI / API / schema changes

## Reporting issues

Open an issue at <https://github.com/loci-knowledge/loci/issues> with steps to
reproduce and the output of `loci --version && loci doctor`.
