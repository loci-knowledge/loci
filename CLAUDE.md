# loci — Claude Code integration

loci is a personal memory graph server. When running, it exposes an HTTP API
on `127.0.0.1:7077` and an MCP server over stdio.

## Quick start

```bash
# installed (uv tool install loci / pipx install loci)
loci config init          # one-time: writes ~/.loci/.env and ~/.loci/config.toml
loci project create <slug>
loci server               # starts HTTP + worker

# from source (dev / clone)
uv sync
# add keys to .env (see loci config init output for the format)
uv run loci server
```

## MCP server (Claude Code)

Register globally (works for both install and clone paths):

```bash
# installed binary (primary path)
claude mcp add loci --transport stdio --scope user -- loci mcp

# from source clone
claude mcp add loci --transport stdio --scope user -- \
  uv run --directory /Users/r4yen/repos/loci loci mcp
```

The server shows up in every Claude Code session automatically (user scope = all dirs).
Verify with: `! claude mcp get loci`

### Choosing which loci project to use

Three options, first match wins:

**Option A — per-workspace `.mcp.json` (recommended for pinned projects)**

Create a `.mcp.json` in your project folder (e.g. `~/Documents/my-research/`):

```json
{
  "mcpServers": {
    "loci": {
      "type": "stdio",
      "command": "loci",
      "args": ["mcp"],
      "env": { "LOCI_PROJECT": "your-slug" }
    }
  }
}
```

Claude Code prompts for approval the first time.

**Option B — bind the directory**

```bash
cd ~/Documents/my-research
loci project bind your-slug   # writes .loci/project.toml here
```

MCP tools walk up the directory tree to find `.loci/project.toml`. Commit it if you want the binding tracked in git.

**Option C — pin for the session**

```bash
loci current set your-slug    # writes ~/.loci/state/current
loci current show             # verify
loci current clear            # unpin
```

This is picked up by every MCP session that has no other resolution path.

Also works: pass `project=` explicitly in each tool call, or set `LOCI_PROJECT` env var.

## Key MCP tools

| tool | what it does |
|------|-------------|
| `loci_retrieve` | semantic + lex search over a project's sources |
| `loci_draft` | generate a cited markdown draft |
| `loci_expand_citation` | get the full body of a cited raw node |
| `loci_expand_node` | get all three locus slots for an interpretation node |
| `loci_propose_node` | author a new interpretation (relation_md / overlap_md / source_anchor_md) |
| `loci_accept_proposal` | accept a housekeeping proposal from reflect |
| `loci_reflect` | run reflect (pass `absorb=True` to run absorb checkpoint first) |
| `loci_research` | enqueue autoresearch job (paper search + optional sandbox) |
| `loci_research_status` | poll an autoresearch job |
| `loci_feedback` | submit citation-level feedback on a draft |
| `loci_context` | get project profile + live loci summary for this session |
| `loci_current_project` | resolve which project is active in this session |
| `loci_workspace_*` | create / list / link / unlink workspaces and add sources |

## Key CLI commands

```bash
loci config init                         # first-run: writes ~/.loci/.env + config.toml
loci doctor                              # show all resolved paths and active project
loci project create <slug>               # interactive setup wizard
loci project manage                      # manage existing projects
loci project bind <slug>                 # write .loci/project.toml in cwd
loci current set <slug>                  # pin project for MCP sessions
loci workspace scan <ws>                 # index / re-index sources
loci kickoff <project>                   # seed the interpretation graph
loci draft <project> "..."               # draft with citations
loci retrieve <project> "..."            # retrieval (alias: q)
loci reflect <project>                   # run reflection cycle
loci reflect <project> --absorb          # absorb checkpoint then reflect
loci research <project> <ws> "..."       # autoresearch (blocking)
loci rebuild <project>                   # re-derive all loci (keep raws)
loci export [<project>]                  # write graph.json + memo.md snapshots
loci graph export <project>              # export D3 HTML visualization
loci reset                               # wipe everything
```

## Storage layout

All user data lives under `~/.loci/` regardless of install method:

```
~/.loci/
  loci.sqlite          # the graph database
  blobs/               # content-addressed raw files
  models/              # embedding model cache (~130 MB, downloaded on first scan)
  assets/              # D3 cache for graph export
  logs/loci.log        # rotating application log (server/mcp/worker)
  exports/             # default graph export destination
  research/            # autoresearch artifacts (fallback; prefers <repo>/.loci/research/)
  state/current        # pinned project for MCP sessions
  .env                 # provider keys (chmod 600; written by loci config init)
  config.toml          # non-secret settings (model IDs, weights, etc.)
  version.json         # layout version stamp
```

Per-repo binding (git-trackable, text-only):

```
<your-repo>/.loci/
  project.toml         # { slug = "...", created_at = "..." }
  .gitignore           # auto-generated; views/research/drafts are opt-in to commit
  views/graph.json     # optional: loci export snapshot
  views/memo.md        # optional: loci export snapshot
  research/<run_id>/   # autoresearch artifacts (preferred location)
```

## Architecture in brief

Three layers:
1. **Raw nodes** — content-addressed files (PDF, md, code, …) with embeddings
2. **Interpretation nodes** — loci of thought: `relation_md`, `overlap_md`, `source_anchor_md`
3. **Project** — a profile + membership view over the graph

Edges: `cites` (interp→raw) and `derives_from` (interp→interp). Strict DAG.

Retrieval is interpretation-routed: loci are scored first, then their cited
raws are promoted. Response includes `routing_loci[]` (side context) and
`trace_table[]` (per-raw routing path).

Source layout:
```
src/loci/
  ui/         # CLI (ui/cli.py) and TUI (ui/tui.py) — entry: loci.ui.cli:main
  usecases/   # shared orchestration: retrieve.py, draft.py
  api/        # FastAPI REST + WebSocket
  mcp/        # MCP adapter
  graph/      # node/edge/project/workspace repositories
  retrieve/   # lex + vec + hyde + PPR pipeline
  draft.py    # draft pipeline (domain module)
  jobs/       # background queue + worker
  ingest/     # walk → hash → extract → embed
  config.py   # settings + all ~/.loci/ path properties
  layout.py   # data-dir version stamp + lazy migrations
  logging_config.py  # rotating file handler setup
```

See `docs/` for full architecture, graph model, and agent behaviour.
