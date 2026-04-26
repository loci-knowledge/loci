# loci — Claude Code integration

loci is a personal memory graph server. When running, it exposes an HTTP API
on `127.0.0.1:7077` and an MCP server over stdio.

## Quick start

```bash
uv sync                   # install deps
cp .env.example .env      # add your provider key (OPENAI_API_KEY or OPENROUTER_API_KEY)
uv run loci server        # starts HTTP + worker
```

## MCP server (Claude Code)

The loci MCP server is registered globally via:

```bash
claude mcp add loci --transport stdio --scope user -- \
  uv run --directory /Users/r4yen/repos/loci loci mcp
```

It shows up in every Claude Code session automatically (user scope = all dirs).
Check it with: `! claude mcp get loci`

### Choosing which loci project to use

The MCP server is a long-running process with a fixed cwd (the loci repo), so
`.loci/project` walk-up won't reach your workspace folder. Use one of these:

**Option A — per-workspace `.mcp.json` (recommended)**

Create a `.mcp.json` in your project folder (e.g. `~/Documents/my-research/`):

```json
{
  "mcpServers": {
    "loci": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/Users/r4yen/repos/loci", "loci", "mcp"],
      "env": { "LOCI_PROJECT": "your-slug" }
    }
  }
}
```

This overrides the global user config for that folder and tells the server
which project to use. Claude Code prompts for approval the first time.

**Option B — pass project= explicitly per call**

```
loci_retrieve("my query", project="your-slug")
```

Use `loci_current_project()` to verify which project is active.

## Key MCP tools

| tool | what it does |
|------|-------------|
| `loci_retrieve` | semantic + lex search over a project's sources |
| `loci_draft` | generate a cited markdown draft |
| `loci_expand_citation` | get the full body of a cited raw node |
| `loci_expand_node` | get all three locus slots for an interpretation node |
| `loci_propose_node` | author a new interpretation (relation_md / overlap_md / source_anchor_md) |
| `loci_accept_proposal` | accept a housekeeping proposal from absorb |
| `loci_absorb` | run periodic graph housekeeping |
| `loci_feedback` | submit citation-level feedback on a draft |
| `loci_context` | get project profile + live loci summary for this session |
| `loci_current_project` | resolve which project is active in this session |
| `loci_workspace_*` | create / list / link / unlink workspaces and add sources |

## Key CLI commands

```bash
uv run loci project create <slug>    # interactive setup wizard
uv run loci project manage           # manage existing projects
uv run loci workspace scan <ws>      # index / re-index sources
uv run loci kickoff <project>        # seed the interpretation graph
uv run loci draft <project> "..."    # draft with citations
uv run loci q <project> "..."        # quick retrieval
uv run loci absorb <project>         # periodic housekeeping
uv run loci rebuild <project>        # re-derive all loci (keep raws)
uv run loci reset                    # wipe everything
uv run loci graph export <project>   # export D3 HTML visualization
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

See `docs/` for full architecture, graph model, and agent behaviour.
