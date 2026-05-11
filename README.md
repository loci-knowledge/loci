# loci

A personal memory server that understands **why** a paper matters — differently
for each project you work on.

You save sources; loci tags them with **aspects** (concepts, methods, topics),
wires **concept edges** between related resources, and — for each project —
generates a **project-scoped interpretation**: what this document means *here*,
right now, for your specific goals.

All of that happens automatically, in the background, without you doing anything
after saving.

```
save → tag → interpret per-project → recall with "why surfaced"
```

---

## What "interpretation layers" means in practice

Suppose you save Knuth's *Literate Programming* paper into loci. It lands in
your global library once. But if you have two projects — `codoc` (building a
documentation tool) and `plm` (studying programming language theory) — loci
generates *two different interpretations*:

**codoc project:**
> *stance: supporting* — "Knuth's WEB system is a strong historical foundation
> for CoDoc: it validates the premise that documentation/specification should be
> a primary programming artifact and that software should be organized for human
> understanding."

**plm project:**
> *stance: methodological* — "Foundational precedent for treating programs as
> communicative artifacts; the 'tangling/weaving' transformation anticipates
> modern macro and metaprogramming systems."

When you do `loci_recall "documentation generation"` inside `codoc`, you get
the first framing. Inside `plm`, you get the second. Same paper, different lens.

---

## Install

```bash
# with uv (recommended)
uv tool install loci-wiki

# with pipx
pipx install loci-wiki
```

The first scan downloads the embedding model (`BAAI/bge-small-en-v1.5`, ~130 MB)
into `~/.loci/models/`.

## Quick start

```bash
# 1. First-run setup: writes ~/.loci/.env (provider keys) and config.toml
loci config init

# 2. Create a project
loci project create my-research

# 3. Register with Claude Code (one-time, user-scope)
claude mcp add loci --transport stdio --scope user -- loci mcp

# 4. Bind a directory so MCP picks up the project automatically
cd ~/Documents/my-research
loci project bind my-research

# 5. Save sources (Claude Code, CLI, or HTTP API)
loci save https://arxiv.org/abs/2312.00001 --folder papers
loci save ~/notes/outline.md --folder notes

# 6. The worker classifies aspects and generates project interpretations
#    automatically — check progress with:
loci status my-research

# 7. Recall in Claude Code
#    loci_recall("documentation generation") → ranked results with project-aware reasons
```

All user data lives under `~/.loci/`. Run `loci doctor` to see resolved paths.

---

## How the pipeline works (no user action required)

```
ingest (save / scan)
    └─→ classify_aspects         # global tags: "literate programming", "WEB system"
            └─→ infer_interpretation (per project, daily bucket)
                    ├─→ project_resource_aspects  # "Historical foundation for CoDoc"
                    ├─→ project_interpretations   # stance + 2-sentence summary
                    └─→ concept_edges             # typed relations to other resources
```

Triggers that refresh interpretations (all automatic):

| Trigger | When |
|---|---|
| `loci_save` | Immediately after ingesting a new resource |
| `loci_recall` | Daily bucket per resource per project |
| `loci_browse` | Daily bucket per browsed resource |
| Usage threshold | After 5 recall hits per project |
| Content change | On re-scan when file hash changes |
| Profile update | When you edit the project profile |
| Conversation hook | When Claude Code session context is relevant |

---

## User workflow in Claude Code

### Start a session

```
loci_context
```

Shows resource count, top aspects, and workspace links for your current project.

### Save something

```
loci_save("https://arxiv.org/abs/2312.00001")
```

loci suggests a folder and aspects via an elicitation form. Confirm or edit.
Interpretation is queued immediately — no daily rate limit on first save.

### Recall with project-aware reasons

```
loci_recall("how do other tools handle documentation freshness?")
```

Returns ranked resources. Each result includes a **why surfaced** reason that
reads the resource through your project's lens:

> **CodeWiki** `papers`  
> *Why surfaced: In this project (supporting): CodeWiki evaluates whether LLMs
> can produce holistic documentation — directly comparable to CoDoc's scope.*

### Edit aspects on a resource

```
loci_aspects(resource_id="01KQDB5E...", action="add", labels=["freshness", "evaluation"])
```

User-set labels are gold: the LLM never overwrites them. A `refresh_project_edges`
job runs automatically to update co-aspect edges.

### Browse your library

```
loci_browse(folder="papers", aspect="retrieval-augmented generation")
```

Returns a markdown table. All browsed resources get usage events that feed back
into the interpretation pipeline.

### @-mention a specific resource

```
@loci:source://01KQDB5ECMH0YYVZS61X0KMHA9
```

Pulls the full body into context. Usage event fires; interpretation may refresh
overnight.

---

## CLI commands

```bash
loci config init                              # write ~/.loci/.env + config.toml
loci doctor                                   # storage paths + active project
loci server                                   # HTTP API + worker on 127.0.0.1:7077
loci mcp                                      # MCP stdio server (for Claude Code)
loci worker                                   # background worker only

loci project create <slug>                    # interactive wizard
loci project list / info <slug> / bind <slug>
loci current set/clear/show <slug>            # pin project for MCP sessions

loci workspace create / list / add-source / scan / link / unlink
loci scan <project>                           # scan all linked workspaces

loci save <url_or_path> [--folder F] [--aspects a,b]
loci recall "query" [--aspects a,b] [--folder F] [-n 10]
loci aspects [resource_id] [--add a --remove b --list-vocab]

loci status [project]
loci export [project]
loci reset                                    # wipe everything
```

---

## MCP surface (Claude Code)

| tool | what it does |
|---|---|
| `loci_save` | ingest URL/file/text, propose folder + aspects, write to DB |
| `loci_recall` | concept-expand + BM25/ANN + graph rerank, return project-aware reasons |
| `loci_aspects` | list or edit aspects on a resource (gold-protected user edits) |
| `loci_browse` | list resources filtered by folder/aspect/keyword |
| `loci_context` | project profile + resource count + top aspects |
| `loci_research` | paper-search sub-agent (v1.1) |

@-mentionable resources:

```
@loci:source://{resource_id}     full body of one resource
@loci:folder://{folder_path}     list of resources in that folder
@loci:aspect://{label}           list of resources tagged with that aspect
```

---

## Passive inference via Claude Code hooks (optional)

loci can infer interpretations from your Claude Code session without any
explicit tool calls. Set `capture_conversation = true` in `~/.loci/config.toml`,
then add to your `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [{"hooks": [{"type": "command",
      "command": "loci event conversation --role user"}]}],
    "Stop": [{"hooks": [{"type": "command",
      "command": "loci event conversation --role assistant"}]}]
  }
}
```

loci fuzzy-matches your prompts against project aspects; if relevant resources
are detected, it queues interpretation refreshes overnight.

---

## Source layout

```
src/loci/
  ui/cli.py              CLI entry point + TUI wizard
  api/                   FastAPI REST routes
  mcp/                   FastMCP server, project resolution, session tracking
  graph/                 aspects, concept_edges, interpretations, projects, workspaces
  retrieve/              BM25 + ANN + HyDE + concept expansion + pipeline
  capture/               ingest, folder/aspect suggestion, link parsing
  ingest/                walk → hash → extract → chunk → embed
  jobs/                  queue + worker + handlers
    classify_aspects.py  global tagging → seeds project interpretation cascade
    infer_interpretation.py  per-project LLM interpretation (daily-bucketed)
    refresh_project_edges.py  cheap co-aspect edge recompute after user edits
    sweep_interpretations.py  periodic stale-row enqueuer
  embed/                 sentence-transformers wrapper
  llm/                   pydantic-ai wrapper
  db/                    schema.sql + incremental migrations
  config.py              Settings + ~/.loci/ paths
```

## License

MIT.
