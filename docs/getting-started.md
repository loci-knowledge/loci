# Getting started

Imagine you have a folder of mixed PDFs, code, and notes — you want loci to
build a memory graph over them, then a town-style visualization in VSCode for
exploring it. This walks you all the way through.

We'll use a real example folder throughout: `~/Documents/codoc/` — three roots
side-by-side under one parent:

```
~/Documents/codoc/
  papers/   # PDFs of research I'm reading
  code/     # open-source projects I'm studying
  notes/    # my own working notes (markdown)
```

You can substitute your own folder anywhere you see `codoc`.

## The two pieces

You're standing up two repos:

- **`loci/`** — the server. Owns SQLite, the embedding model, the agent. Talks
  HTTP/WS on `127.0.0.1:7077` and MCP over stdio.
- **`loki-frontend/`** — a VSCode extension that opens a "Town" panel. Talks to
  the loci server. *Optional* — everything works from the CLI; the extension
  is a richer UI when you want one.

You always start `loci` first. The extension connects to it.

## 0. Install loci

loci targets Python 3.12+. We use [uv](https://docs.astral.sh/uv/) for
dependency management.

```bash
git clone https://github.com/<you>/loci.git
cd loci
uv sync                 # creates .venv with the runtime deps
uv sync --extra dev     # add test/lint deps if you want to run pytest
```

This installs `pydantic-ai-slim`, `sqlite-vec`, `sentence-transformers`,
FastAPI, and friends. The first scan downloads the embedding model
(`BAAI/bge-small-en-v1.5`, ~130 MB) into `~/.loci/models/`.

For Apple Silicon, MPS is auto-detected. For CUDA, set
`LOCI_EMBEDDING_DEVICE=cuda`.

## 1. Configure provider keys

loci runs without LLM keys (retrieval, FTS, scan all work LLM-free), but the
LLM-dependent features — drafting, kickoff, the silent reflection cycle, HyDE
— degrade to no-ops. Set at least one provider key in `.env` at the repo
root:

```bash
# .env (any one of these is enough; loci will pick whichever the model spec
# points at — see step 1b)
OPENAI_API_KEY=sk-...
OPENROUTER_API_KEY=sk-or-...
ANTHROPIC_API_KEY=sk-ant-...
```

### 1b. Pick your models (defaults are OpenAI)

The four model roles default to:

```
interpretation_model = openrouter:google/gemini-3-flash-preview  # builds + maintains the interp layer
rag_model            = openrouter:google/gemini-3-flash-preview  # synthesises drafts
classifier_model     = openrouter:deepseek/deepseek-v4-flash     # contradiction classifier in absorb
hyde_model           = openrouter:deepseek/deepseek-v4-flash     # hypothetical-doc expansion
```

Override any of them in `.env`:

```bash
LOCI_INTERPRETATION_MODEL=anthropic:claude-opus-4-7
LOCI_RAG_MODEL=openrouter:google/gemini-3-pro
```

Full guide: [model-config.md](./model-config.md).

## 2. Create your project

A project is a *view* over the global graph: a profile, the set of nodes
included, and the agent's voice anchor. One PDF can participate in many
projects without duplication.

The profile is the seed for kickoff and the agent's "what are we doing
here?" prompt. Keep it 50–300 words and write it from the user's
perspective — what you want from loci, not a description of the files.

```bash
cat > /tmp/codoc-profile.md <<'EOF'
# codoc — research project

I'm investigating how documentation, code, and notes intermix in real
codebases — especially for tools that bridge code and natural language
(deepwiki-open, codenav-vscode), with the goal of designing better
"code-as-document" UX.

The vault has three roots, organised by *modality*:
- `papers/` — published research I'm drawing on (PDFs).
- `code/`   — open-source projects I'm reading and learning from.
- `notes/`  — my own working notes, including a paper-in-progress.

What I want from loci:
1. Surface conceptual links across modalities — e.g. how a UIST paper's
   claim about navigation relates to a specific function in the code, or
   how my own RR plan responds to a reviewer point.
2. When I draft new text, cite spans inside files, not just file names.
3. Build interpretations that compress how I actually think about this
   work — not summaries of every paper.

Style: concise, technical. Prefer interpretations as short claims with
evidence pointers, not prose blurbs.
EOF

uv run loci project create codoc \
  --name "Code-as-Document" \
  --profile /tmp/codoc-profile.md
# → created codoc (01KQ2AGY2T146QMDSF5QMFVJ7A)
```

Save that ULID — the frontend uses it. (You can always look it up later
with `uv run loci project list`.)

## 3. Create a workspace and add sources

A **workspace** is a named collection of source roots. It sits between your
filesystem and a project, so the same scanned files can serve multiple
projects without re-scanning.

```bash
uv run loci workspace create codoc-ws \
  --name "Codoc sources" \
  --kind mixed
```

`kind` is one of `papers | codebase | notes | transcripts | web | mixed`. Use
`mixed` when a workspace spans more than one modality (as in our case).

Now register the three roots:

```bash
uv run loci workspace add-source codoc-ws ~/Documents/codoc/papers --label papers
uv run loci workspace add-source codoc-ws ~/Documents/codoc/code   --label code
uv run loci workspace add-source codoc-ws ~/Documents/codoc/notes  --label notes
```

Supported file types: PDF, Markdown, plain text, RST/org, HTML, transcripts
(VTT/SRT), and ~30 source-code extensions. See [sources.md](./sources.md)
for the full list and high-quality PDF parsing via marker.

## 4. Link the workspace to your project

```bash
uv run loci workspace link codoc-ws codoc --role primary
```

Roles: `primary` (the workspace's content drives this project's context),
`reference` (supplementary; included but weighted lower), or `excluded`
(explicitly suppressed). A project can link multiple workspaces — one might
be `primary`, others `reference`.

Linking with `--role primary` automatically enqueues a `relevance` job that
does a focused single-pass synthesis for the workspace↔project pair. This
runs in the background; you can proceed without waiting for it.

> **Tip — sharing a workspace across projects.** If you have a second project
> that should see the same raws (say, a `codoc-review` project a colleague is
> using), just link the same workspace there:
> ```bash
> uv run loci workspace link codoc-ws codoc-review --role reference
> ```
> The workspace is scanned once and both projects see the same raw nodes via
> the `project_effective_members` view — no re-scanning, no duplication.

## 5. Scan

```bash
uv run loci workspace scan codoc-ws
```

This walks every source registered to the workspace, content-hashes each
file, deduplicates against the global raw store, extracts text, batches
embeddings through the local model, and writes one `RawNode` per file.
Sample output:

```
{
  'scanned': 131, 'new_raw': 131, 'deduped': 0,
  'members_added': 131, 'errors': []
}
```

Re-run `loci workspace scan codoc-ws` whenever you add files — it's
idempotent. Files already present (by content hash) are skipped without
re-extraction. Because the workspace is linked to the project, newly scanned
nodes immediately become part of `codoc`'s effective members.

## 6. Kickoff: seed the interpretation graph

Kickoff reads your profile + a sample of the raws and generates 5–8
*relationship observations* — `relevance`, `philosophy`, and `decision`
nodes that capture **how the workspace content connects to the project's
goals**. They land at confidence 0.5, directly into the live graph.

Unlike open questions, these seed the graph with actionable observations:
"these codebases show the server-as-CLI pattern this project needs" is more
useful on day one than "what should the CLI look like?"

```bash
uv run loci kickoff codoc --n 6
# → result: { 'skipped': false, 'observations_written': 6,
#            'model': 'openrouter:google/gemini-3-flash-preview' }
```

The observations show up in retrieval immediately alongside raw sources.

Note: if the `relevance` job from the workspace link (step 4) has not yet
finished, kickoff will still work — it draws from whatever raws are already
scanned.

## 7. Draft something

Now the high-leverage operation. Ask loci to write something using your
sources:

```bash
uv run loci draft codoc \
  "Synthesize what CoDoc, codenav-vscode, and Knuth's literate programming
   each say about the relation between code and prose. Where do they agree,
   where do they diverge?" \
  --k 12
```

You'll get markdown with inline `[C1]`, `[C2]`, … citations that map to
specific nodes (PDFs, code files, notes), followed by a `citations[]` block.
Each citation includes the node id, kind, title, and *why* it was retrieved
(which signals matched).

**While you read the draft**, a `reflect` job auto-enqueues. The agent reads
your task + the citations the draft used, and (silently, in the worker
thread) decides whether to add new interpretation nodes, reinforce existing
ones, or soften ones that didn't help. Background; non-blocking.

By the time you come back tomorrow, the graph has a few new live
interpretation nodes you didn't write. See [agent.md](./agent.md) for what
the agent is allowed to do.

## 8. Close the alignment loop with feedback

If you edit the draft (kept these citations, dropped those, rewrote the
sentence around C2…) and submit your edit:

```bash
loci feedback <response_id> /path/to/your-edit.md
```

loci diffs the `[Cn]` markers, emits per-citation traces (kept / dropped /
replaced), then enqueues a follow-up reflection that aligns the
interpretation layer with how you actually used the draft. Citations you
kept reinforce the underlying nodes; ones you dropped soften them.

This is the core alignment loop. Three or four cycles in, the agent's voice
starts to sound like yours.

## 9. Start the server

For the VSCode extension, MCP clients, or the REST API, run the server:

```bash
uv run loci server
# → worker thread started
# → Uvicorn running on http://127.0.0.1:7077
```

The HTTP API is at `http://127.0.0.1:7077/docs` (FastAPI auto-generated).

For Claude Code MCP integration:

```bash
uv run loci mcp        # stdio transport — Claude subprocesses this
```

To let MCP tools auto-resolve the current project without passing a `project`
argument every call, bind a project to your working directory:

```bash
loci project bind codoc   # writes .loci/project in the cwd
```

MCP tools walk up from cwd to find this file. You can also set
`LOCI_PROJECT=codoc` in your environment, or pass `project=` explicitly.

## 10. Connect the VSCode extension (loki-frontend)

The extension lives in a separate repo. Once the loci server is running on
127.0.0.1:7077, the extension picks it up automatically.

```bash
git clone https://github.com/<you>/loki-frontend.git
cd loki-frontend
npm install
npm run build       # builds extension/ + webview/
```

Then open the `loki-frontend` folder in VSCode and press F5 to launch the
Extension Development Host. In the new window:

1. Cmd+Shift+P → **"Loci: Open Town"**.
2. The first time, you get a project picker — select **Code-as-Document**
   (or whatever you slugged your project as). Selection is remembered in
   workspace settings.
3. The Town panel opens. The webview subscribes via WebSocket and renders
   nodes as villagers, communities as districts, pinned interpretations on
   pedestals, and traces as villager animations (walking to the council
   plaza when a citation fires).

Configure the server URL or pre-pin a project in VSCode settings:

```jsonc
{
  "lokiTown.serverUrl": "http://127.0.0.1:7077",
  "lokiTown.projectId": "01KQ2AGY2T146QMDSF5QMFVJ7A"
}
```

Full extension guide: [frontend.md](./frontend.md).

## 11. Maintain: absorb (occasionally)

Every ~30 sessions or once a week, run absorb to consolidate:

```bash
uv run loci absorb codoc
```

What absorb does — periodic *housekeeping*, not the primary maintenance
surface (the silent reflect cycle handles per-draft work):

- replays trace logs into `access_count` / `confidence`
- audits orphan nodes, broken `cites` (raws gone missing), bloat
- alias-detection over interpretation nodes (cosine > 0.92 → propose merge)
- forgetting candidates (no access in N days + low confidence)
- contradiction pass (LLM-mediated; needs an API key)
- community detection (Leiden; needs `loci[graph]` extra)
- semantic edge refresh for co-cited interpretation pairs (step 9)
- code dependency extraction: Python/JS/TS import analysis → `actual` edges (step 10)

## What's next

- [frontend.md](./frontend.md) — the VSCode extension in depth.
- [agent.md](./agent.md) — what the silent reflection cycle actually does
  to your graph.
- [architecture.md](./architecture.md) — the three layers; how files flow.
- [model-config.md](./model-config.md) — picking provider/model per task.
- [sources.md](./sources.md) — file format support, marker setup.
- [session-lifecycle.md](./session-lifecycle.md) — months-later view.
