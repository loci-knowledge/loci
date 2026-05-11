# loci — Claude Code integration

loci is a personal memory server that generates **project-scoped interpretations**
of your saved resources. Each resource is tagged globally with aspects, then
re-interpreted in the context of each project: what does this document mean *here*,
for your specific research goals?

All of this happens automatically in the background from MCP usage — no explicit
classification commands needed.

## Quick start

```bash
# installed (uv tool install loci-wiki / pipx install loci-wiki)
loci config init               # one-time: writes ~/.loci/.env + ~/.loci/config.toml
loci project create <slug>
loci server                    # HTTP + worker on 127.0.0.1:7077

# from source (dev / clone)
uv sync
# add provider keys to .env (see `loci config init` for the format)
uv run loci server
```

## MCP server (Claude Code)

Register globally — works for both install and clone paths:

```bash
# installed binary (primary path)
claude mcp add loci --transport stdio --scope user -- loci mcp

# from source clone
claude mcp add loci --transport stdio --scope user -- \
  uv run --directory /path/to/loci loci mcp
```

Verify with `! claude mcp get loci`.

### Choosing which loci project to use

First match wins:

**A — per-workspace `.mcp.json`** (recommended for pinned projects)

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

**B — bind the directory**

```bash
cd ~/Documents/my-research
loci project bind your-slug    # writes .loci/project.toml
```

MCP tools walk up the directory tree to find `.loci/project.toml`. Commit it
if you want the binding tracked in git.

**C — pin for the session**

```bash
loci current set your-slug     # writes ~/.loci/state/current
loci current show
loci current clear
```

You can also pass `project=` explicitly in each tool call, or set
`LOCI_PROJECT` in the environment.

## MCP tools (6)

| tool | what it does |
|---|---|
| `loci_save` | ingest a URL / file / text, propose folder + aspects via elicitation, persist |
| `loci_recall` | concept-expand the query, BM25 + ANN + graph-rerank, return project-aware reasons |
| `loci_aspects` | list or edit aspects on a resource; user edits are gold (never overwritten) |
| `loci_browse` | list resources filtered by folder / aspect / keyword |
| `loci_context` | project profile + resource count + top aspects for the current session |
| `loci_research` | paper-search sub-agent (stub; v1.1) |

## MCP resources (@-mentionable)

```
@loci:source://{resource_id}     full body of a single resource
@loci:folder://{folder_path}     list of resources in that folder
@loci:aspect://{label}           list of resources tagged with that aspect
```

## What fires automatically after every MCP call

All MCP tools call `record_event()` which:
1. Writes a `resource_usage_log` row (project_id, session_hash, query, tool).
2. Enqueues `infer_interpretation` with a daily-bucket fingerprint
   `infer_interpretation:{project_id}:{resource_id}:{YYYY-MM-DD}`.

This means: recall something 10 times in a day → exactly one LLM call overnight.

The interpretation pipeline:
```
classify_aspects  (after ingest)       global tags → "literate programming", "WEB system"
    ↓ cascades to
infer_interpretation (per project, daily)
    → project_resource_aspects         "Historical foundation for CoDoc"
    → project_interpretations          stance + 2-sentence project-aware summary
    → concept_edges                    typed relations: supports, instantiates, …
```

## CLI commands

```bash
loci config init                              # ~/.loci/.env + config.toml
loci doctor                                   # storage paths + active project
loci server                                   # HTTP + worker
loci mcp                                      # MCP stdio (use via `claude mcp add`)
loci worker                                   # background worker only

loci project create <slug>                    # interactive wizard
loci project list / info <slug> / bind <slug>
loci current set/clear/show <slug>            # pin for MCP sessions

loci workspace create / list / add-source / scan / link / unlink
loci scan <project>                           # scan all linked workspaces

loci save <url_or_path> [--folder F] [--aspects a,b]
loci recall "query" [--aspects a,b] [--folder F] [-n 10]
loci aspects [resource_id] [--add a --remove b --list-vocab]

loci event conversation --role user           # pipe Claude Code hook payload via stdin
loci status [project]
loci export [project]
loci reset                                    # wipe everything
```

## Storage layout

All user data lives under `~/.loci/`:

```
~/.loci/
  loci.sqlite          single-file database (source of truth)
  blobs/               content-addressed raw files (sha256-keyed)
  models/              embedding model cache (~130 MB, downloaded on first scan)
  logs/loci.log        rotating application log
  exports/             default destination for `loci export`
  state/current        pinned project for MCP sessions
  .env                 provider keys (chmod 600; written by `loci config init`)
  config.toml          non-secret settings
```

Per-repo binding (git-trackable):

```
<your-repo>/.loci/
  project.toml         { slug = "...", created_at = "..." }
  .gitignore           auto-generated; opt in to commit views/
  views/               optional `loci export` snapshots
```

## Architecture in brief

Two aspect layers on top of raw sources + embeddings:

```
nodes / raw_nodes / raw_chunks       chunks_fts + chunk_vec       (lex + ANN)
resource_aspects                     global tags, seeded at ingest
project_resource_aspects             per-project, LLM-interpreted, gold-protected
project_interpretations              stance + summary per (project, resource)
concept_edges                        citations, co-aspect, supports, contradicts…
projects / project_workspaces        workspace_membership         (scoping)
jobs                                 background queue
conversation_events                  Claude Code hook signals      (optional)
```

Retrieval:
```
query
  → expand_query_aspects (fuzzy+embedding over vocab + concept_edges neighbors, project-scoped)
  → HyDE (grounded by project.profile_md + expanded aspects)
  → BM25 over chunks_fts  +  ANN over chunk_vec  +  aspect-overlap soft channel (3-way RRF)
  → RRF fusion (k=60, aspect channel weight=0.5)
  → graph rerank (signed-sum, edge-weight-aware: supports+0.30, contradicts-0.15, …)
  → aspect-density resource bonus (α=0.2)
  → group + materialise
  → build_why_surfaced: uses merged_aspects; prepends project interpretation summary when available
```

Source layout:
```
src/loci/
  ui/cli.py            CLI (entry: loci.ui.cli:main) + ui/tui.py wizard
  api/                 FastAPI app + routes (projects, workspaces, sources, aspects, jobs)
  mcp/                 MCP server, project resolution, session.py, events.py
  graph/               sources, aspects, concept_edges, interpretations, projects, workspaces
  retrieve/            lex + vec + hyde + concept_expand + pipeline
  capture/             ingest, folder_suggest, aspect_suggest (+ classify_project_interpretation_llm)
  ingest/              walk → hash → extract → chunk → embed
  jobs/                queue + worker + handlers
    classify_aspects.py         global tagging → cascades to infer_interpretation
    infer_interpretation.py     per-project LLM interpretation (daily-bucketed)
    refresh_project_edges.py    cheap co-aspect edge recompute after user aspect edits
    sweep_interpretations.py    periodic stale-row enqueuer (14-day threshold)
  embed/               sentence-transformers wrapper
  llm/                 pydantic-ai wrapper
  db/                  schema.sql + migrations.py
  config.py            Settings + ~/.loci/ paths
```

See `docs/` for the user-facing guide and the architecture deep dive.
