# Auto-research

loci can run a research sub-agent that crawls the literature, optionally
executes code in a sandboxed HuggingFace Space, and automatically ingests
its findings back into your project graph — so future `loci_retrieve` /
`loci_draft` calls can cite papers and code experiments just like local
sources.

## What it does

```
query ──► paper search ──► read methodology ──► save_paper_note
               │
               └──► citation graph / recommend ──► more papers
               └──► code experiment (optional, sandbox only) ──► save_code
               │
               └──► ResearchReport
                         │
                         ▼
               scan_workspace  (artifacts → raw nodes)
                         │
                         ▼
               relevance locus (cites all artifact raws)
                         │
                         ▼
               loci_retrieve / loci_draft  ←──── now includes research
```

A single job (`autoresearch`) handles the full pipeline:

1. Run the pydantic-ai research agent for up to `max_iterations` turns.
   Paper APIs (HF, Semantic Scholar, arXiv) are always available.
2. *Optional:* spin up an HF Spaces sandbox for code execution
   (only when `sandbox=True` + `hf_owner` are provided).
3. Agent saves artifacts: `papers/<id>.md`, `notes/*.md`, `code/*`, `SUMMARY.md`.
4. `scan_workspace` ingests all artifacts as raw nodes.
5. A `relevance` locus is created that cites every artifact raw and bridges
   the findings to your project profile.
6. A follow-up `relevance` job deepens interpretation coverage.

## Prerequisites

| What | Where |
|------|-------|
| LLM key (any provider) | `.env` — `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, or `ANTHROPIC_API_KEY` |
| An existing workspace with at least one source root | `loci workspace scan <ws>` run at least once |
| HF token *(sandbox only)* | `HF_TOKEN=hf_...` in `.env` |
| Semantic Scholar key *(optional)* | `S2_API_KEY=...` in `.env` — raises rate limits |

The agent can run without `HF_TOKEN` — it just won't have code execution.
It can also run without `S2_API_KEY` — it falls back to HuggingFace Papers
for search.

## Configuration

All settings are in `Settings` (prefix `LOCI_`):

```bash
# Which model to use for the research agent
LOCI_RESEARCH_MODEL=openrouter:anthropic/claude-opus-4.6

# HF username/org for sandbox Spaces (required when sandbox=true)
LOCI_RESEARCH_HF_OWNER=your-hf-username

# HF Spaces hardware tier (default cpu-basic; upgrade for GPU workloads)
LOCI_RESEARCH_SANDBOX_HARDWARE=cpu-basic

# Template Space to duplicate (any duplicable Space works)
LOCI_RESEARCH_TEMPLATE_SPACE=burtenshaw/sandbox

# HF API token (no LOCI_ prefix — shared with huggingface_hub)
HF_TOKEN=hf_...

# Semantic Scholar API key (no LOCI_ prefix)
S2_API_KEY=...
```

`research_model` follows the same `<provider>:<model>` spec as the other
model settings (see [model-config.md](./model-config.md)).

## CLI

```bash
# Basic — paper search only (no sandbox required)
uv run loci research my-project my-workspace "what are the best algorithms for personalized PageRank?"

# With sandbox code execution
uv run loci research my-project my-workspace \
    "benchmark PPR vs HNSW on 1M-node graph" \
    --hf-owner your-hf-username

# Override hardware tier
uv run loci research my-project my-workspace \
    "train a small GNN on citation data" \
    --hf-owner your-hf-username \
    --hardware t4-small

# Limit agent iterations (default 30)
uv run loci research my-project my-workspace "..." --max-iterations 10
```

On completion the CLI prints the artifact paths and the `summary_locus_id`
that now lives in your graph.

## MCP tools

### `loci_research`

Enqueues an `autoresearch` job and returns immediately with a `job_id`.
The job runs in the loci background worker.

`sandbox` defaults to `False` — paper discovery needs no HF account.
Set `sandbox=True` + `hf_owner` only when you want the agent to run code.

```python
result = loci_research(
    query="what compression methods work best for knowledge graphs?",
    workspace="my-workspace",       # workspace slug
    project="my-project",           # optional — resolved from context
    # sandbox=False by default — paper-only, no HF account needed
    # sandbox=True, hf_owner="your-username"  # to enable code execution
    max_iterations=30,              # optional
)
# result["job_id"] — pass to loci_research_status
```

### `loci_research_status`

Poll the job for live progress and final results:

```python
status = loci_research_status(job_id="<job_id>")
```

Response fields:

| Field | Description |
|-------|-------------|
| `status` | `queued` \| `running` \| `done` \| `failed` |
| `progress` | Float 0.0–1.0 |
| `progress_display` | Pre-formatted progress bar + last 12 agent steps — display directly to the user |
| `step_log` | Raw array of `{t, tool, msg}` entries; one per tool call |
| `result` | Full result dict once `status='done'` (see below) |
| `error` | Error message if `status='failed'` |

**`progress_display` example** while running:

```
[███░░░░░░░] 30%  running
  🔍 searching: graph neural networks for knowledge compression
  📄 paper details: 2310.04795
  📖 reading 2310.04795 §3
  🕸 citation graph: 2310.04795
  💾 saving: 2310.04795
  🔍 searching: knowledge graph embedding quantization survey
```

**Recommended polling loop** (Claude Code or any MCP client):

```python
import time

result = loci_research(query="...", workspace="my-workspace")
job_id = result["job_id"]

while True:
    status = loci_research_status(job_id=job_id)
    print(status["progress_display"])           # show live steps to user
    if status["status"] in ("done", "failed"):
        break
    time.sleep(12)

if status["status"] == "done":
    print(status["result"]["summary_md"])
```

**`result` dict** (when `status='done'`):

| Key | Value |
|-----|-------|
| `output_dir` | Absolute path to `<source_root>/research/<run_id>/` |
| `artifacts` | Relative paths of every written file |
| `artifact_node_ids` | Raw node IDs in loci |
| `summary_locus_id` | ID of the `relevance` interpretation node |
| `summary_md` | Narrative summary of findings |
| `used_papers` | arXiv IDs that materially informed the summary |
| `sandbox_url` | HF Space URL (if sandbox was used), else `null` |
| `iterations` | LLM turns taken |
| `scan` | `{new_raw, deduped, skipped}` from the workspace re-scan |

## Intermediate step visibility

Every tool call the agent makes is appended to `step_log` (persisted in the
`jobs.step_log` column). `loci_research_status` returns both the raw log and
a pre-formatted `progress_display` string.

Step labels are human-readable:

| Tool called | Label shown |
|-------------|------------|
| `search_papers("graph RAG")` | `🔍 search papers: graph RAG` |
| `paper_details("2305.14283")` | `📄 paper details: 2305.14283` |
| `read_paper("2305.14283", "3")` | `📖 reading 2305.14283 §3` |
| `citation_graph("2305.14283")` | `🕸 citation graph: 2305.14283` |
| `snippet_search("attention free transformer")` | `🔎 snippet search: attention free transformer` |
| `save_paper_note("2305.14283", ...)` | `💾 saving: 2305.14283` |
| `sandbox_bash("python train.py")` | `⚡ exec: python train.py` |

The log is capped at 60 entries; `progress_display` shows the last 12.

## Artifact layout

Artifacts land under the first source root of the workspace:

```
<source_root>/
└── research/
    └── <run_id>/           # 8-char hex run ID
        ├── papers/
        │   ├── 2401.00001.md
        │   └── 2312.99999.md
        ├── notes/
        │   └── summary-notes.md
        ├── code/            # only present when sandbox was used
        │   └── ppr_benchmark.py
        └── SUMMARY.md      # narrative takeaways + paper table
```

Every file in `<run_id>/` becomes a raw node in loci (via `scan_workspace`).
A `relevance` locus is created that cites all of them and carries the
research summary as its `relation_md`, so retrieval can route to any
artifact from a thematic query.

## Available tools inside the agent

### Paper discovery (always available)

| Tool | What it does |
|------|-------------|
| `search_papers(query, ...)` | HuggingFace Papers by default; routes to Semantic Scholar when filters are set (`date_from`, `categories`, `min_citations`, `sort_by`) |
| `trending_papers(date?, query?)` | Daily trending papers from HF Papers; optional keyword filter |
| `paper_details(arxiv_id)` | Metadata + abstract + S2 enrichment (citation count, open access, TLDR) |
| `read_paper(arxiv_id, section?)` | Full sections from ArXiv HTML. Omit `section` for TOC + abstract; pass a section title to read it. |
| `citation_graph(arxiv_id, direction?, limit?)` | References + citations with influence flags |
| `snippet_search(query, limit?)` | Semantic Scholar full-text passage search across 12M+ papers |
| `recommend(arxiv_id?, positive_ids?, negative_ids?)` | Find similar papers via S2 recommendations API |
| `find_datasets(arxiv_id)` | HuggingFace datasets linked to a paper |
| `find_models(arxiv_id)` | HuggingFace models linked to a paper |
| `find_collections(arxiv_id)` | HuggingFace collections containing a paper |
| `find_all_resources(arxiv_id)` | Parallel fetch of datasets + models + collections |

### Artifact saving (always available)

| Tool | What it saves |
|------|--------------|
| `save_paper_note(arxiv_id, body_md, title?)` | `papers/<arxiv_id>.md` — title, arxiv URL, key claims, method summary |
| `save_note(filename, body_md)` | `notes/<filename>.md` — arbitrary research note |

### Code execution (only when `sandbox=True` + `hf_owner` set)

| Tool | What it does |
|------|-------------|
| `sandbox_bash(command, work_dir?, timeout?)` | Run a shell command in the HF Space; returns stdout + stderr |
| `sandbox_read(path, offset?, limit?)` | Read a file from the sandbox |
| `sandbox_write(path, content)` | Write a file in the sandbox |
| `sandbox_edit(path, old_str, new_str, replace_all?)` | Targeted edit of a sandbox file |
| `save_code(filename, body)` | Save code as a `code/<filename>` artifact |

## How findings enter retrieval

After the job completes:

1. Every `papers/*.md`, `notes/*.md`, `code/*`, and `SUMMARY.md` file is a
   **raw node** in the graph with full-text embeddings.
2. A **`relevance` locus** (interpretation node) is created with:
   - `relation_md` — the research summary (what was found and why it matters).
   - `cites` edges to all artifact raws.
3. A follow-up `relevance` job scores each artifact raw against the project
   profile and creates additional targeted loci for the most relevant ones.

From here the normal retrieval flow applies:

```python
# After research completes, these all work:
loci_retrieve("personalized PageRank approximate methods", project="my-project")
loci_draft("compare graph retrieval approaches for my use case", project="my-project")
```

The research locus will appear in `routing_loci[]` when its topic matches the
query. Paper notes appear as citeable raws.

## Graceful degradation

| Missing | Effect |
|---------|--------|
| No LLM key | Job returns `skipped=True` immediately; no artifacts written |
| No `HF_TOKEN` / no `hf_owner` | Sandbox tools absent; paper search + artifact saving still work |
| No `S2_API_KEY` | `snippet_search`, `citation_graph`, `recommend` fall back to HF-only; `search_papers` without filters still works |
| `sandbox=True` but sandbox creation fails | Warning logged; agent continues without code execution tools |
| `read_paper` returns no HTML | Graceful empty string; agent falls back to abstract from `paper_details` |

## Example session (Claude Code + MCP)

```python
# Start a research job — no sandbox needed for paper discovery
result = loci_research(
    query="survey approximate nearest-neighbour search methods for graphs",
    workspace="my-research",
    project="knowledge-graph",
)
job_id = result["job_id"]

# Poll and show live steps to the user
import time
while True:
    s = loci_research_status(job_id=job_id)
    # Display the progress bar + recent steps directly to user:
    # [████░░░░░░] 40%  running
    #   🔍 search papers: approximate nearest-neighbour graphs
    #   📄 paper details: 2401.12345
    #   📖 reading 2401.12345 §3
    print(s["progress_display"])
    if s["status"] in ("done", "failed"):
        break
    time.sleep(12)

# Use findings in retrieval as normal
loci_draft(
    "What indexing methods should I use for my graph retrieval layer?",
    project="knowledge-graph",
)
```

## Costs and rate limits

| Service | Rate limit | Notes |
|---------|-----------|-------|
| HF Papers | ~10 req/s (unofficial) | No key required |
| Semantic Scholar search | 1 req/s (unauthenticated) | Add `S2_API_KEY` for 10 req/s |
| Semantic Scholar other endpoints | 10 req/s | Shared limit with search |
| ArXiv HTML | Polite crawl, no stated limit | loci adds 0.5 s delay between reads |
| HF Spaces (sandbox) | 1 concurrent free Space | Sandbox is deleted after the job |

loci's S2 client has a 500-entry in-memory LRU cache and a 3-retry
exponential backoff; you won't burn your rate limit on repeated lookups
within a single research run.

A typical 30-iteration run with 5 papers deeply read costs roughly:
- ~40 Semantic Scholar requests
- ~10 ArXiv HTML fetches
- 1 HF Space lifecycle (create → use → delete) if sandbox enabled
- LLM cost: depends on model; with `claude-opus-4.6` via OpenRouter, expect
  ~$0.05–0.20 per run depending on paper depth.
