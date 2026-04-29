# Architecture

A short tour of how loci is wired together.

## Three things to remember

1. **Sources are leaves.** Each source is content-addressed (sha256), chunked,
   and embedded. SQLite holds metadata + FTS + vectors; raw blobs live on disk.
2. **Aspects are tags.** Every source carries one or more aspect labels
   (`methodology`, `knowledge-graph`, …). Aspects come from three places:
   the folder it lives in, the LLM classifier, and the user.
3. **Concept edges are the graph.** Typed edges connect sources:
   citations (extracted from BibTeX), wikilinks (extracted from markdown),
   `co_aspect` (sources sharing aspects), `co_folder` (sources sharing a
   folder). Retrieval expands the query through this graph.

## Storage

```
~/.loci/
  loci.sqlite     SQLite database — single source of truth
  blobs/          raw files, content-addressed: <sha256[:2]>/<sha256[2:]>
  models/         embedding model cache (BAAI/bge-small-en-v1.5)
  logs/           rotating application log
  exports/        default graph.json / memo.md target
  state/current   pinned project for MCP sessions
  .env            provider keys (chmod 600)
  config.toml     non-secret settings
```

Per-repo:

```
<repo>/.loci/
  project.toml    { slug, created_at }
  session.toml    optional: workspaces bound for this session
```

## Schema

`src/loci/db/schema.sql` is the single canonical schema. It is applied
idempotently on every connect via `init_schema()` — every statement uses
`CREATE … IF NOT EXISTS`. There is no migration history. When the schema
changes we rewrite the file and consumers run `loci reset`.

Core tables:

| table | purpose |
|-------|---------|
| `nodes` / `raw_nodes` | source rows (id, title, sha256, blob path, …) |
| `raw_chunks` | chunked text (one row per chunk, with offsets) |
| `chunks_fts` / `chunk_vec` | FTS5 + sqlite-vec virtual tables over chunks |
| `nodes_fts` / `node_vec` | document-level FTS + vec for whole-source matches |
| `aspect_vocab` / `resource_aspects` | aspect taxonomy + per-resource tags |
| `concept_edges` | typed edges (cites, wikilink, co_aspect, co_folder) |
| `resource_provenance` | url, folder, saved_via, captured_at, context_text |
| `resource_usage_log` | one row per `loci_*` MCP tool call (for ranking signals) |
| `projects` / `project_membership` | project + membership rows |
| `information_workspaces` / `workspace_sources` / `project_workspaces` | workspace plumbing |
| `jobs` / `job_step_log` | background queue |

## Module layout

```
src/loci/
  ui/         CLI (cyclopts) + interactive wizard
  api/        FastAPI app + REST routes + WebSocket
  mcp/        MCP server (FastMCP) + project resolution
  graph/      sources, aspects, concept_edges, projects, workspaces
  retrieve/   lex, vec, hyde, concept_expand, pipeline
  capture/    URL/file/text ingest, folder + aspect suggestion, link parsing
  ingest/     walker, content-hash, extractors, chunker, chunks repo, pipeline
  jobs/       queue, worker, handlers (classify_aspects, parse_links, log_usage, embed_missing)
  embed/      sentence-transformers wrapper
  llm/        pydantic-ai wrapper (Anthropic, OpenAI, OpenRouter)
  db/         schema.sql + connection helpers
  config.py   Settings + ~/.loci/ paths
```

Each subpackage owns one concern end-to-end. Imports flow inward: `ui` and
`mcp` and `api` may import from `graph` / `retrieve` / `capture` / `jobs`,
but never the other way.

## Save flow

```
loci_save("https://arxiv.org/abs/...", context="...")
  ├─ ingest_url   → fetch, extract text, sha256
  │                  raw_nodes + blob written
  ├─ chunker      → raw_chunks (with offsets)
  ├─ embedder     → chunk_vec + node_vec
  ├─ folder_suggest (rapidfuzz against existing folders)
  ├─ aspect_suggest (KeyBERT over first chunks → aspect_vocab match)
  ├─ MCP elicitation form: folder radio + aspect checkboxes + context text
  │                  resource_provenance + resource_aspects written
  ├─ project_membership.add
  └─ enqueue jobs:
       classify_aspects   (LLM-driven aspect refinement)
       parse_links        (wikilinks + BibTeX citations → concept_edges)
```

## Recall flow

```
loci_recall("how does PPR work")
  ├─ concept_expand:
  │    KeyBERT(query) → seed concepts → traverse concept_edges
  │    → expanded aspect set
  ├─ HyDE: rag_model produces a hypothetical answer for vec embedding
  ├─ BM25 over chunks_fts (query + expanded aspect filter)
  ├─ ANN over chunk_vec  (HyDE-embedded query)
  ├─ RRF merge → candidate chunks
  ├─ Graph rerank:
  │    boost chunks whose source shares aspects with the seed concepts
  │    pull in chunks reachable via cites / wikilink edges from top hits
  └─ return ranked chunks with per-chunk "why surfaced":
       "matched aspects [methodology, ppr]; reached via co_aspect from
        HippoRAG → PPR paper"
```

## Background jobs

The worker (`loci worker`, also embedded inside `loci server`) drains the
`jobs` table. Handlers are registered in `jobs/worker.py`:

| handler | trigger | what it does |
|---------|---------|--------------|
| `embed_missing` | scan, save | embed any chunks lacking vectors |
| `classify_aspects` | save | call `rag_model` to refine aspect tags + confidence |
| `parse_links` | save | extract wikilinks (obsidiantools) + BibTeX citations (pybtex) → write `concept_edges` |
| `log_usage` | every `loci_*` MCP call | append to `resource_usage_log` |

## MCP surface

Six tools, all stdio:

```
loci_save        ingest + elicit folder/aspects + write
loci_recall      concept-expanded retrieval
loci_aspects     list/edit aspects (action="list"|"add"|"remove"|"edit")
loci_browse      list resources, filterable by folder / aspect / query
loci_context     project profile + counts + top aspects
loci_research    paper-search sub-agent (deferred to v1.1)
```

Three resource templates for `@`-mention in Claude Code:

```
loci:source://{resource_id}
loci:folder://{folder_path}
loci:aspect://{label}
```

## LLMs

`llm/` is a thin wrapper over pydantic-ai. Each task has its own model spec:

- `rag_model` — strong instruction following (aspect classification, HyDE
  rewriting). Default `openrouter:anthropic/claude-opus-4.7`.
- `hyde_model` — fast, throwaway hypothetical answers.
  Default `openrouter:deepseek/deepseek-v4-flash`.

Provider keys live in `~/.loci/.env`; loci falls back through
`OPENROUTER_API_KEY` → `OPENROUTER_API_KEY_BACKUP` if the primary looks
invalid.

## Extending loci

- **New aspect source?** Implement a job handler that writes to
  `resource_aspects` with `source='inferred'` (or your own tag) and queue it
  from `capture/ingest.py`.
- **New edge type?** Append to `concept_edges` with a new `edge_type` value;
  update the rerank weights in `retrieve/pipeline.py`.
- **New extractor?** Add to `ingest/extractors.py` keyed by mimetype/extension.
- **New CLI command?** Add to `ui/cli.py` under the `app` cyclopts namespace.

The schema rule of thumb: if it changes the canonical model, edit
`db/schema.sql` and bump the loci version. Otherwise keep changes in code.
