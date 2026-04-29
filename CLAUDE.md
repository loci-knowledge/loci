# loci — Claude Code integration

loci is a personal memory server. When running, it exposes an HTTP API on
`127.0.0.1:7077` and an MCP server over stdio. Saved sources are tagged with
aspects (folder-inferred + LLM-inferred + user-edited) and connected by
typed edges (citations, wikilinks, co-aspect). Retrieval expands the query
through that concept graph and returns ranked chunks with "why surfaced"
reasons.

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
`LOCI_PROJECT` directly in the environment.

## MCP tools (6)

| tool             | what it does                                                                   |
|------------------|--------------------------------------------------------------------------------|
| `loci_save`      | ingest a URL / file / text, propose folder + aspects via elicitation, persist  |
| `loci_recall`    | concept-expand the query, run BM25 + ANN over chunks, graph-rerank, return reasons |
| `loci_aspects`   | list or edit aspects on a resource (elicitation form for edits)                |
| `loci_browse`    | list resources with their folder + top aspects, filter by folder/aspect/query  |
| `loci_context`   | project profile + resource count + top aspects for the current session         |
| `loci_research`  | paper-search sub-agent (stub; v1.1)                                            |

## MCP resources (@-mentionable)

```
@loci:source://{resource_id}     full body of a single resource
@loci:folder://{folder_path}     list of resources in that folder
@loci:aspect://{label}           list of resources tagged with that aspect
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
loci use [workspace_slugs...] [--project p]   # set active context (rich table)

loci save <url_or_path> [--folder F] [--aspects a,b]
loci recall "query" [--aspects a,b] [--folder F] [-n 10]
loci aspects [resource_id] [--add a --remove b --list-vocab]

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

Single layer: raw sources, embedded at chunk granularity, joined to a concept
graph of aspects + typed edges.

```
nodes / raw_nodes / raw_chunks       chunks_fts + chunk_vec   (lex + ANN)
aspect_vocab / resource_aspects      concept_edges            (concept graph)
projects / project_workspaces        workspace_membership     (scoping)
jobs                                                          (background work)
```

Retrieval:

```
query
  → expand_query_aspects (rapidfuzz over aspect_vocab + concept_edges neighbors)
  → HyDE
  → BM25 over chunks_fts  +  ANN over chunk_vec
  → RRF fusion
  → graph rerank (boost chunks whose resource neighbors top hits via co_aspect / cites)
  → group + materialise + build "why surfaced" reason per resource
```

Source layout:

```
src/loci/
  ui/cli.py            CLI (entry: loci.ui.cli:main) + ui/tui.py wizard
  api/                 FastAPI app + routes (projects, workspaces, sources, aspects, jobs)
  mcp/                 MCP server + project resolution
  graph/               sources, aspects, concept_edges, projects, workspaces
  retrieve/            lex + vec + hyde + concept_expand + pipeline
  capture/             ingest, folder_suggest, aspect_suggest, link_parser
  ingest/              walk → hash → extract → chunk → embed
  jobs/                queue + worker + handlers
  embed/               sentence-transformers wrapper
  llm/                 pydantic-ai wrapper
  db/                  schema.sql + connection.py
  config.py            Settings + ~/.loci/ paths
```

See `docs/` for the user-facing guide and the architecture deep dive.
