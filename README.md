# loci

A personal memory **DAG**. Raw sources are leaves. Interpretations are *loci
of thought* — pointers that say "the part of THIS source over here meets the
part of THIS project over there, in this specific way." Loci route a query
to the parts of sources that matter; they never hold the answer themselves.

Two directed edge types — `cites` (interp → raw) and `derives_from` (interp
→ interp) — make the graph acyclic and the provenance trivially traceable.
Retrieval routes through loci to surface raws plus a per-raw trace; drafts
cite raws only and ship a routing-locus side panel for inspection.

Served to any client (Claude Code via MCP, a VSCode extension, the CLI,
plain HTTP) with a uniform citation contract. See [`docs/graph.md`](./docs/graph.md)
for the locus model and [`docs/architecture.md`](./docs/architecture.md) for
the full pipeline.

## Status

Early. Single-user, local-first. The architecture in `PLAN.md` is the spec; this
repo implements it.

## Install

```bash
# with uv (recommended — isolated environment)
uv tool install loci

# with pipx
pipx install loci

# with pip
pip install --user loci
```

Python 3.12+ required. The first scan downloads the embedding model
(`BAAI/bge-small-en-v1.5`, ~130 MB) into `~/.loci/models/`.

## Quick start

```bash
# 1. First-run setup: writes ~/.loci/.env (provider keys) and ~/.loci/config.toml
loci config init

# 2. Create a project (interactive wizard)
loci project create my-project

# 3. Start the server
loci server
```

All user data lives under `~/.loci/`. Run `loci doctor` to see all resolved paths.

## Usage

```bash
loci project create <slug>               # interactive wizard: project + workspace + scan + kickoff
loci project bind <slug>                 # bind cwd to project (writes .loci/project.toml)
loci workspace create <name>             # create a workspace
loci workspace add-source <ws> <path>    # register a source root
loci workspace scan <ws>                 # walk + hash + embed all sources
loci kickoff <project>                   # seed the interpretation graph
loci retrieve <project> "query"          # semantic + lex retrieval with routing trace
loci draft <project> "instruction"       # cited markdown draft
loci reflect <project> [--absorb]        # reflection / maintenance cycle
loci server                              # HTTP + MCP server (127.0.0.1:7077)
loci doctor                              # show storage paths + active project
loci export [<project>]                  # write graph.json + memo.md snapshots
loci current set <slug>                  # pin project for MCP sessions without .loci/ binding
```

Full guide: [`docs/getting-started.md`](./docs/getting-started.md).

## MCP (Claude Code)

Register once:

```bash
claude mcp add loci --transport stdio --scope user -- loci mcp
```

Then in any working directory either:
- run `loci project bind <slug>` to write `.loci/project.toml` (git-trackable), or
- run `loci current set <slug>` to pin globally for the session.

See `CLAUDE.md` for the full MCP setup and per-workspace `.mcp.json` pattern.

## Development (from source)

```bash
git clone https://github.com/loci-knowledge/loci.git
cd loci
uv sync
# add keys to .env (same format as ~/.loci/.env)
uv run loci server
```

## Layout

```
src/loci/
  ui/               # CLI (cli.py) and TUI (tui.py)
  usecases/         # shared orchestration per operation (retrieve, draft)
  api/              # FastAPI REST + WebSocket
  mcp/              # MCP adapter
  graph/            # node/edge/project/workspace repositories
  retrieve/         # lex + vec + hyde + PPR
  draft.py          # draft pipeline
  citations/        # trace + response writers
  jobs/             # background queue + worker
  ingest/           # walk → hash → dedup → extract → embed
  llm/              # pydantic-ai wrapper
  config.py         # settings + ~/.loci/ path properties
  layout.py         # data-dir version stamp + lazy migrations
  logging_config.py # rotating file handler
  db/               # schema, migrations, sqlite + sqlite-vec
```

## License

MIT.
