# loci

A personal memory server. You **save** what you read; loci tags each source
with **aspects** (methodology, knowledge-graph, …), wires up **concept edges**
(citations, wikilinks, co-aspect), and serves the result to Claude Code over
**MCP** so the model can recall the parts of your library that actually matter
for what you're writing.

No interpretation graph, no draft pipeline, no PageRank. Just:

```
save → tag → recall
```

If you want a deeper read, start at [`docs/getting-started.md`](./docs/getting-started.md)
and then [`docs/architecture.md`](./docs/architecture.md).

## Status

Single-user, local-first. SQLite is the source of truth; raw blobs are
content-addressed on disk. Python 3.12+.

## Install

```bash
# with uv (recommended — isolated environment)
uv tool install loci-wiki

# with pipx
pipx install loci-wiki

# or just curl-pipe the installer
curl -fsSL https://raw.githubusercontent.com/loci-knowledge/loci/main/install.sh | sh
```

The first scan downloads the embedding model (`BAAI/bge-small-en-v1.5`,
~130 MB) into `~/.loci/models/`.

## Quick start

```bash
# 1. First-run setup: writes ~/.loci/.env (provider keys) and ~/.loci/config.toml
loci config init

# 2. Create a project
loci project create my-research

# 3. Register loci with Claude Code (one-time, user-scope)
claude mcp add loci --transport stdio --scope user -- loci mcp

# 4. Bind a directory to your project (so MCP knows which slug to use)
cd ~/Documents/my-research
loci project bind my-research

# 5. Save sources directly from Claude Code
#    (or via CLI: `loci save https://arxiv.org/abs/1612.03975`)

# 6. Recall in Claude Code
#    `@loci:source://...` for a single resource
#    `loci_recall("how does PPR work")` for ranked chunks with reasons
```

All user data lives under `~/.loci/`. Run `loci doctor` to see resolved paths.

## CLI commands

```bash
loci config init                              # write ~/.loci/.env + config.toml
loci doctor                                   # show storage paths + active project
loci server                                   # HTTP API + worker on 127.0.0.1:7077
loci mcp                                      # MCP stdio server (for Claude Code)
loci worker                                   # background worker only

loci project create <slug>                    # interactive wizard
loci project list / info / bind / manage
loci current set <slug>                       # pin project for MCP sessions

loci workspace create / list / add-source / scan / link / unlink
loci scan <project>                           # scan all linked workspaces
loci use [workspace_slugs...]                 # bind workspaces for this session

loci save <url_or_path> [--folder] [--aspects]
loci recall "query" [--aspects ...] [-n 10]
loci aspects [resource_id] [--add ... --remove ... --list-vocab]

loci status [project]
loci export [project]
loci reset                                    # wipe everything
```

## MCP surface (Claude Code)

| tool                 | what it does                                                                  |
|----------------------|-------------------------------------------------------------------------------|
| `loci_save`          | ingest URL/file/text, propose folder + aspects via elicitation, write to DB   |
| `loci_recall`        | concept-expand + BM25/ANN over chunks with concept-graph rerank               |
| `loci_aspects`       | list/edit aspects on a resource (elicitation form for editing)                |
| `loci_browse`        | list resources with folder + top aspects, filterable                          |
| `loci_context`       | project profile + counts + top aspects for the current session                |
| `loci_research`      | paper-search sub-agent (deferred to v1.1)                                     |

@-mentionable resources:

```
@loci:source://{resource_id}
@loci:folder://{folder_path}
@loci:aspect://{label}
```

## Source layout

```
src/loci/
  ui/cli.py              entry point: loci.ui.cli:main
  api/                   FastAPI REST + WebSocket
    routes/              projects, workspaces, sources, aspects, jobs
  mcp/                   MCP server + project resolution
  graph/                 sources, aspects, concept_edges, projects, workspaces
  retrieve/              lex + vec + hyde + concept_expand + pipeline
  capture/               ingest, folder_suggest, aspect_suggest, link_parser
  ingest/                walk → hash → extract → chunk → embed
  jobs/                  queue + worker + handlers (classify_aspects, parse_links, …)
  embed/                 sentence-transformers wrapper
  llm/                   pydantic-ai wrapper
  db/                    schema.sql + connection.py
  config.py              Settings + ~/.loci/ paths
```

## License

MIT.
