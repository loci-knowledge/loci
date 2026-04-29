# Contributing to loci

## Development setup

```bash
git clone https://github.com/loci-knowledge/loci
cd loci
uv sync --extra dev
cp .env.example .env   # add OPENAI_API_KEY or OPENROUTER_API_KEY
uv run loci server     # verify it starts
```

Run lint + tests before pushing:

```bash
uv run ruff check src/ tests/
uv run pytest -m "not slow and not integration" --tb=short
```

## Project layout

The codebase is organised by concern (each subpackage owns one thing
end-to-end):

```
src/loci/
  ui/         CLI (cyclopts) + interactive wizard
  api/        FastAPI app + REST routes + WebSocket
  mcp/        MCP server (FastMCP) + project resolution
  graph/      sources, aspects, concept_edges, projects, workspaces
  retrieve/   lex + vec + hyde + concept_expand + pipeline
  capture/    URL/file/text ingest + folder/aspect suggestion + link parsing
  ingest/     walker, content-hash, extractors, chunker, chunks repo, pipeline
  jobs/       queue + worker + handlers (classify_aspects, parse_links, …)
  embed/      sentence-transformers wrapper (BAAI/bge-small-en-v1.5)
  llm/        pydantic-ai wrapper (Anthropic / OpenAI / OpenRouter)
  db/         schema.sql + connection helpers
  config.py   Settings + ~/.loci/ paths
```

The schema is a single canonical file (`src/loci/db/schema.sql`) applied
idempotently on every connect via `init_schema()`. There is no migration
history; when the schema changes we rewrite the file and consumers run
`loci reset`.

## Submitting changes

1. Fork the repo and branch from `main`.
2. Keep commits focused; favour many small commits over one giant one.
3. Open a pull request against `main` — CI runs on Ubuntu / macOS / Windows ×
   Python 3.12 / 3.13.
4. A maintainer will review and merge.

## Release process

Releases are triggered by pushing a `v*` tag. The workflow builds the wheel
and sdist, publishes to PyPI via trusted publishing, and creates a GitHub
Release with generated notes.

### One-time PyPI setup (maintainers only)

Before the first release, configure trusted publishing on PyPI:

1. `pip index versions loci-wiki` to confirm the name is yours.
2. On pypi.org go to **Your projects → Publishing → Add a new pending publisher**:
   - Owner: `loci-knowledge`
   - Repository: `loci`
   - Workflow filename: `release.yml`
   - Environment name: `pypi`

### Cutting a release

```bash
uv build
uv tool run twine check dist/*

# Bump version in pyproject.toml, commit, then tag
git tag v0.1.0
git push origin v0.1.0
```

`release.yml` fires on the tag push.

### Version policy

[SemVer](https://semver.org/):

- **patch** (`0.1.x`) — bug fixes, docs, dependency bumps
- **minor** (`0.x.0`) — new features, additive API changes
- **major** (`x.0.0`) — breaking CLI / API / schema changes

## Code style

- Formatter / linter: `ruff` (config in `pyproject.toml`)
- Type hints on all public functions
- No comments unless the *why* is non-obvious
- Imports use `from __future__ import annotations` so all annotations are
  strings (no runtime cost for type-only imports)

## Dependency changes

Runtime deps live in `[project.dependencies]`; dev-only deps in
`[project.optional-dependencies] dev`. Pin conservatively (ranges, not
exact pins) so consumers aren't blocked.

After editing `pyproject.toml`, run `uv lock` and commit `uv.lock`.

## Reporting issues

Open an issue at <https://github.com/loci-knowledge/loci/issues> with steps
to reproduce and the output of `loci --version`.
