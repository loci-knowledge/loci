# Getting started

This walks you from `git clone` to a working project with cited drafts in
about 10 minutes.

## 0. Install

loci targets Python 3.12+. We use [uv](https://docs.astral.sh/uv/) for
dependency management.

```bash
git clone https://github.com/<you>/loci.git
cd loci
uv sync                 # creates .venv with the runtime deps
uv sync --extra dev     # add test/lint deps if you want to run pytest
```

This will install `pydantic-ai-slim`, `sqlite-vec`, `sentence-transformers`,
FastAPI, and friends. The first run downloads the embedding model
(`BAAI/bge-small-en-v1.5`, ~130 MB) on first use, into `~/.loci/models/`.

For Apple Silicon, MPS is auto-detected. For CUDA, set `LOCI_EMBEDDING_DEVICE=cuda`.

## 1. Configure provider keys (optional but recommended)

loci runs without any LLM keys, but the LLM-dependent features (drafting,
HyDE, kickoff, contradiction detection in absorb) degrade to no-ops. Set at
least one provider key in your shell or in a `.env` file at the repo root:

```bash
# pick one (or any combination)
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
export OPENROUTER_API_KEY=sk-or-...
```

By default, all four LLM tasks (interpretation, RAG, classifier, HyDE) point
at Anthropic models. To change them, see [model-config.md](./model-config.md).

## 2. Create your first project

A project is a *view* over the global graph: a profile, a config blob, and
the set of nodes you've explicitly included. One paper can participate in
many projects without duplication.

```bash
# Write a profile first — this becomes the seed for kickoff and is what the
# draft engine sees as the project's "mission". Keep it 50–300 words.
cat > /tmp/profile.md <<'EOF'
# Project: Transformer attention variants

I want to understand how positional information is encoded across attention
variants — sinusoidal, learned positional embeddings, RoPE, ALiBi, etc.
The angle I care about: **where in the architecture** the position lives
(input embedding, projection, attention pattern, output) and what each
choice trades off.

Avoid: history-of-NLP framing. Avoid: tutorial-level explanations.
Prefer: ablation papers, criticism papers, "we tried X and it broke" notes.
EOF

uv run loci project create transformer-attention \
  --name "Transformer Attention Variants" \
  --profile /tmp/profile.md
```

The slug `transformer-attention` is how you'll address the project from now on
(the CLI also accepts the ULID it printed).

## 3. Tell loci where your files are

Files can live anywhere on your filesystem. You register *roots* with the
project; `loci scan` walks every registered root.

```bash
uv run loci source add transformer-attention ~/papers/attention/  --label "papers"
uv run loci source add transformer-attention ~/notes/attention/    --label "notes"
uv run loci source add transformer-attention ~/code/transformer-lab/ --label "code"

uv run loci source list transformer-attention
```

Supported file types out of the box: PDF, Markdown, plain text, RST/org,
HTML, transcripts (VTT/SRT), and ~30 source-code extensions. See
[sources.md](./sources.md) for the full list and how to add more.

## 4. Scan

```bash
uv run loci scan transformer-attention
```

This walks every registered root, content-hashes each file, deduplicates
against the global raw store (so the same PDF in two projects becomes two
memberships of one underlying node), extracts text, batches embeddings
through the local model, and writes `RawNode`s. Output:

```
{
  'scanned': 47, 'new_raw': 47, 'deduped': 0,
  'skipped': 0, 'members_added': 47, 'errors': []
}
```

Re-run `loci scan` whenever you add files — it's idempotent. Files already
present (by content hash) are skipped without re-extraction.

## 5. Kickoff: get the first questions

The bootstrap step. loci reads your `profile.md` plus a sample of the raws
you just scanned, and proposes 5–10 *open questions* worth pursuing in this
project. **It does not invent interpretations on day one** — that's the
"no fabricated interpretations on day 1" rule from the design.

```bash
uv run loci kickoff transformer-attention --n 8
```

You'll see something like:

```
{
  'status': 'done',
  'result': {'skipped': false, 'proposals': 8, 'model': 'anthropic:claude-opus-4-7'}
}
```

The questions land as **proposals**, not live nodes. Review them:

```bash
uv run loci status transformer-attention
# → proposals  pending   8

# To see them:
curl http://localhost:7077/projects/<project_id>/proposals    # if server running
```

Accept the ones you actually want via the REST API or the MCP tool
`loci_accept_proposal`. (CLI command coming next.)

## 6. Query the graph

```bash
uv run loci q transformer-attention "how does rotary attention encode position?"
```

You'll get a ranked table of nodes (raw + interpretation), with `score` and a
short `why` string explaining how each match arose: which lex terms matched,
how near in vector space, whether it's reachable from your pinned anchors via
the graph (Personalized PageRank).

`loci q` writes a `Response` row + per-node `Trace` rows so the absorb job
can later replay them into `access_count` / `confidence`.

## 7. Draft something with citations

```bash
uv run loci draft transformer-attention \
  "Summarize the rotary embedding insight, citing the papers I've read." \
  --style prose --cite-density normal
```

The output is markdown with inline `[C1]`-style citations that map to nodes.
A `citations[]` block follows, listing for each citation: the node id, kind
(raw/interpretation), title, why it was cited, and any `raw_supports` (the
raw papers an interpretation draws on).

If the configured `rag_model` provider has no key, draft returns a stub
showing the candidates it *would* have used, so you still see what loci
retrieved. Set the key, re-run.

## 8. Run the server (for clients like Claude Code)

```bash
uv run loci server
# → Uvicorn on http://127.0.0.1:7077
# → worker thread started (handles absorb, kickoff jobs)
```

The HTTP API is documented at `http://127.0.0.1:7077/docs` (FastAPI's
auto-generated OpenAPI UI). For Claude Code, point your MCP client at:

```bash
# stdio transport — Claude Code subprocesses this
uv run loci mcp
```

The MCP server exposes the curated subset from `PLAN §Open questions`:
`loci_retrieve`, `loci_draft`, `loci_expand_citation`, `loci_expand_node`,
`loci_propose_node`, `loci_accept_proposal`, `loci_absorb`.

## 9. Maintain: absorb

After ~15 sessions, run absorb to consolidate:

```bash
uv run loci absorb transformer-attention
```

What absorb does (PLAN §Background jobs):
- replays trace logs into `access_count` / `confidence`
- audits orphan nodes, broken `cites` (raws gone missing), bloat
- alias-detection over interpretation nodes (cosine > 0.92 → propose merge)
- forgetting candidates (no access in N days + low confidence)
- contradiction pass (LLM-mediated; needs an API key)
- community detection (Leiden; needs `loci[graph]` extra)

Each pass is idempotent. The absorb job is **enqueued** by `loci absorb` and
runs synchronously in the CLI; in server mode the worker thread picks it up.

## What's next

- [architecture.md](./architecture.md) — the mental model.
- [model-config.md](./model-config.md) — pointing each task at a different
  provider/model.
- [sources.md](./sources.md) — file format support, marker setup, formats
  loci can't yet read.
- [session-lifecycle.md](./session-lifecycle.md) — how a project evolves over
  weeks/months.
