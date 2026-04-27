# Getting started

Imagine you have a folder of mixed PDFs, code, and notes — you want loci to
build a memory graph over them, then query it from the CLI or Claude Code.
This walks you through a complete setup.

We'll use a real example folder throughout: `~/Documents/codoc/` — three roots
side-by-side under one parent:

```
~/Documents/codoc/
  papers/   # PDFs of research I'm reading
  code/     # open-source projects I'm studying
  notes/    # my own working notes (markdown)
```

You can substitute your own folder anywhere you see `codoc`.

## 0. Install loci

loci requires Python 3.12+.

### Recommended — isolated install via uv

```bash
uv tool install loci
```

Or with pipx / pip:

```bash
pipx install loci
pip install --user loci
```

After install, `loci` is on your PATH. Verify with:

```bash
loci --version
loci doctor    # shows all resolved paths — useful for debugging
```

### From source (development / contributor)

```bash
git clone https://github.com/loci-knowledge/loci.git
cd loci
uv sync
# all commands below work with `uv run loci <cmd>` from the repo root
```

## 1. First-run setup

Run the interactive setup wizard once:

```bash
loci config init
```

This writes:
- `~/.loci/.env` — your provider API keys (chmod 600, never committed)
- `~/.loci/config.toml` — optional non-secret defaults (model IDs, weights)

It walks you through adding at least one LLM provider key. loci works without
keys for retrieval, scan, and FTS, but drafting, kickoff, and reflect need one.

### Provider keys (manual)

If you prefer to write the file yourself, `~/.loci/.env` format:

```bash
# At least one of these:
OPENAI_API_KEY=sk-...
OPENROUTER_API_KEY=sk-or-...
ANTHROPIC_API_KEY=sk-ant-...

# Optional extras
HF_TOKEN=hf_...          # needed for autoresearch sandbox
S2_API_KEY=...           # raises Semantic Scholar rate limit
```

### Model overrides (optional)

In `~/.loci/config.toml` or `~/.loci/.env`:

```toml
# ~/.loci/config.toml
loci_interpretation_model = "openai:gpt-4o-mini"
loci_rag_model = "openrouter:anthropic/claude-opus-4.7"
loci_classifier_model = "openrouter:deepseek/deepseek-v4-flash"
loci_hyde_model = "openrouter:deepseek/deepseek-v4-flash"
```

Full guide: [model-config.md](./model-config.md).

## 2. Create your project

A **project** is a *view* over the global graph: a profile, the set of nodes
included, and the agent's voice anchor. One PDF can participate in many
projects without duplication.

### Interactive wizard (recommended)

```bash
loci project create codoc
```

The wizard walks you through every step:

```
╭─────────────────────────────────╮
│ loci — personal memory graph    │
│ Interactive project setup       │
╰─────────────────────────────────╯

── Step 1  Project name ────────────────────────────
  Project name [codoc]: Code-as-Document
  Slug [code-as-document]: codoc

── Step 2  Profile ─────────────────────────────────
  Profile file (path to .md, or Enter to skip):

── Step 3  Workspace ───────────────────────────────
  Information workspace:
  > Set up from a folder (recommended)
    Link an existing workspace
    Create a workspace manually
    Skip for now

  Workspace root folder: ~/Documents/codoc

  Found 3 subfolders:
  [x] papers/  (47 files)
  [x] code/    (312 files)
  [x] notes/   (14 files)

  ✓ Created workspace codoc-ws (kind: mixed)
  ✓ Added sources: papers, code, notes

── Step 4  Review ──────────────────────────────────
  Apply, or change something?
  > Apply — create project and scan
    ...

── Applying ────────────────────────────────────────
  ✓ Project codoc created
  ✓ Workspace codoc-ws linked (primary)
  ✓ Scan: 373 new, 0 deduped, 5 skipped
  ✓ Kickoff: 6 observations written

Next: loci server  →  then query with `loci retrieve codoc "..."`
```

The fastest path to a working project: supply your project folder as the
workspace root and the wizard adds each subfolder as a labeled source.

Other wizard entry points:
- **Manage all projects**: `loci project manage` — arrow-key menu.

### Non-interactive (scripted) setup

Pass `--yes` to bypass the wizard:

```bash
loci project create codoc \
  --name "Code-as-Document" \
  --profile /path/to/codoc-profile.md \
  --yes
# → created codoc (01KQ2AGY2T146QMDSF5QMFVJ7A)
```

The profile is the seed for kickoff and the agent's "what are we doing
here?" prompt. Keep it 50–300 words, written from your perspective — what you
want from loci, not a description of the files.

## 3. Create a workspace and add sources

> **If you used the wizard** (step 2) and linked an existing workspace, you
> can skip this step.

A **workspace** is a named collection of source roots. The same scanned files
can serve multiple projects without re-scanning.

```bash
loci workspace create codoc-ws --name "Codoc sources" --kind mixed
```

`kind` is one of `papers | codebase | notes | transcripts | web | mixed`.

Register the roots:

```bash
loci workspace add-source codoc-ws ~/Documents/codoc/papers --label papers
loci workspace add-source codoc-ws ~/Documents/codoc/code   --label code
loci workspace add-source codoc-ws ~/Documents/codoc/notes  --label notes
```

Supported file types: PDF, Markdown, plain text, RST/org, HTML, transcripts
(VTT/SRT), and ~30 source-code extensions. See [sources.md](./sources.md).

## 4. Link the workspace to your project

```bash
loci workspace link codoc-ws codoc --role primary
```

Roles: `primary` (drives the project's context), `reference` (supplementary),
or `excluded`. A project can link multiple workspaces.

## 5. Scan

```bash
loci workspace scan codoc-ws
```

Walks every source root, content-hashes each file, deduplicates against the
global store, extracts text, embeds, writes one `RawNode` per file.
Re-run whenever you add files — it's idempotent.

## 6. Kickoff: seed the interpretation graph

```bash
loci kickoff codoc --n 6
```

Reads your profile + a sample of the raws and generates 5–8 relationship
observations (`relevance`, `philosophy`, `decision` nodes). They appear in
retrieval immediately.

## 7. Start the server

```bash
loci server
# → worker thread started
# → Uvicorn running on http://127.0.0.1:7077
# → logs: ~/.loci/logs/loci.log
```

The HTTP API is at `http://127.0.0.1:7077/docs`.

## 8. Retrieve and draft

```bash
# retrieval with routing trace
loci retrieve codoc "what is the rotary embedding insight?"

# cited markdown draft
loci draft codoc \
  "Synthesize what CoDoc and Knuth's literate programming say about
   code and prose. Where do they agree?" \
  --k 12
```

Draft output includes inline `[C1]`, `[C2]`, … citations that map to specific
nodes, followed by a `citations[]` block. After each draft, a `reflect` job
auto-enqueues silently.

## 9. MCP integration (Claude Code)

Register loci as a global MCP server once:

```bash
claude mcp add loci --transport stdio --scope user -- loci mcp
```

### Project auto-resolution

MCP tools need to know which loci project to work on. Three options:

**Option A — bind the directory** (git-trackable)

```bash
cd ~/Documents/my-research
loci project bind codoc   # writes .loci/project.toml here
```

MCP tools walk up the directory tree to find `.loci/project.toml`.

**Option B — pin for the session**

```bash
loci current set codoc    # writes ~/.loci/state/current
loci current show         # verify
loci current clear        # unpin
```

**Option C — per-workspace `.mcp.json`** (pin for a specific workspace)

```json
{
  "mcpServers": {
    "loci": {
      "type": "stdio",
      "command": "loci",
      "args": ["mcp"],
      "env": { "LOCI_PROJECT": "codoc" }
    }
  }
}
```

Also works: pass `project=` explicitly per call, or set `LOCI_PROJECT` env var.

## 10. Connect the VSCode extension (loki-frontend)

Once `loci server` is running on `127.0.0.1:7077`, the loki-frontend VSCode
extension connects automatically. Full guide: [frontend.md](./frontend.md).

## 11. Maintenance: reflect + absorb

Periodically run a reflect+absorb cycle to consolidate:

```bash
loci reflect codoc --absorb
```

What absorb does:
- replays trace logs into `access_count` / `confidence`
- audits orphan nodes, broken citations, bloat
- alias-detection (cosine > 0.92 → propose merge)
- forgetting candidates (inactive + low confidence)
- contradiction pass (LLM-mediated)
- community detection (needs `loci[graph]` extra)

## Storage reference

All user data lives under `~/.loci/`:

```
~/.loci/
  loci.sqlite          # the graph (nodes, edges, projects, workspaces, jobs, traces)
  blobs/               # content-addressed raw file bytes
  models/              # embedding model (~130 MB, downloaded on first scan)
  assets/              # D3 cache for graph export
  logs/loci.log        # rotating application log (10 MB × 5)
  exports/             # default `loci graph export` output
  research/            # autoresearch artifacts (fallback)
  state/current        # pinned project slug for MCP sessions
  .env                 # provider keys (chmod 600)
  config.toml          # non-secret defaults
  version.json         # layout version stamp
```

Per-repo binding (committed to git if desired):

```
<your-repo>/.loci/
  project.toml         # { slug = "...", created_at = "..." }
  .gitignore           # auto-generated
  views/graph.json     # optional: loci export snapshot
  views/memo.md        # optional: loci export snapshot
  research/<run_id>/   # autoresearch artifacts (preferred over ~/.loci/research/)
```

## What's next

- [frontend.md](./frontend.md) — the VSCode extension.
- [agent.md](./agent.md) — what the silent reflection cycle does to your graph.
- [architecture.md](./architecture.md) — the three layers; how files flow.
- [model-config.md](./model-config.md) — picking provider/model per task.
- [sources.md](./sources.md) — file format support, marker setup.
- [session-lifecycle.md](./session-lifecycle.md) — months-later view.
