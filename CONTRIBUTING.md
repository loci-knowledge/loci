# Contributing to loci

## Development setup

```bash
git clone https://github.com/loci-knowledge/loci
cd loci
uv sync --extra dev
cp .env.example .env   # add OPENAI_API_KEY or OPENROUTER_API_KEY
uv run loci server     # verify it starts
```

Run tests and lint before pushing:

```bash
uv run ruff check src/ tests/
uv run pytest -m "not slow and not integration" --tb=short
```

## Submitting changes

1. Fork the repo and create a branch from `main`.
2. Make your changes; keep commits focused.
3. Open a pull request against `main` — CI runs automatically (Ubuntu / macOS / Windows × Python 3.12 / 3.13).
4. A maintainer will review and merge.

## Release process

Releases are triggered by pushing a `v*` tag. The workflow builds the wheel and sdist, publishes to PyPI via trusted publishing, and creates a GitHub Release with generated notes.

### One-time PyPI setup (maintainers only)

Before the first release, configure trusted publishing on PyPI:

1. Check whether the `loci` name is available: `pip index versions loci`
2. If taken, update `name` in `pyproject.toml` to a unique distribution name.
3. On pypi.org go to **Your projects → Publishing → Add a new pending publisher** and enter:
   - Owner: `loci-knowledge`
   - Repository: `loci`
   - Workflow filename: `release.yml`
   - Environment name: `pypi`

### Cutting a release

```bash
# Verify the build is clean locally
uv build
uv run twine check dist/*

# Bump version in pyproject.toml, commit, then tag
git tag v0.1.0
git push origin v0.1.0
```

The `release.yml` workflow fires on the tag push and handles the rest.

### Version policy

loci follows [Semantic Versioning](https://semver.org/):

- **patch** (`0.1.x`) — bug fixes, docs, dependency bumps
- **minor** (`0.x.0`) — new features, backwards-compatible API additions
- **major** (`x.0.0`) — breaking CLI/API/schema changes

## Code style

- Formatter/linter: `ruff` (config in `pyproject.toml`)
- Type hints on all public functions
- No comments unless the *why* is non-obvious

## Dependency changes

Runtime deps live in `[project.dependencies]`; dev-only deps in `[project.optional-dependencies] dev`. Pin conservatively (ranges, not exact pins) so consumers aren't blocked.

## Reporting issues

Open an issue at <https://github.com/loci-knowledge/loci/issues> with steps to reproduce and the output of `loci --version`.
