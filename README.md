# loci

A personal memory **DAG**. Raw sources are leaves. Interpretations are *loci
of thought* — pointers that say "the part of THIS source over here meets the
part of THIS project over there, in this specific way." Loci route a query
to the parts of sources that matter; they never hold the answer themselves.

Two directed edge types — `cites` (interp → raw) and `derives_from` (interp
→ interp) — make the graph acyclic and the provenance trivially traceable.
Retrieval routes through loci to surface raws plus a per-raw trace; drafts
cite raws only and ship a routing-locus side panel for inspection.

Served to any client (Claude Code via MCP, a VSCode extension, the CLI,
plain HTTP) with a uniform citation contract. See [`docs/graph.md`](./docs/graph.md)
for the locus model and [`docs/architecture.md`](./docs/architecture.md) for
the full pipeline.

## Status

Early. Single-user, local-first. The architecture in `PLAN.md` is the spec; this
repo implements it.

## Quick start

```bash
uv sync
uv run loci project create transformer-attention --profile ./profile.md
uv run loci workspace create transformer-attention-ws --kind mixed
uv run loci workspace add-source transformer-attention-ws ~/papers/transformers --label papers
uv run loci workspace link transformer-attention-ws transformer-attention --role primary
uv run loci workspace scan transformer-attention-ws
uv run loci kickoff transformer-attention   # generate the first loci of thought
uv run loci server                          # start the HTTP/MCP server
uv run loci q transformer-attention "what is the rotary embedding insight?"
```

If you've upgraded across the DAG migration and want a clean slate:

```bash
uv run loci reset                 # drops loci.db (with confirm) + blobs
# then re-run the workspace + project + kickoff sequence above

# or, to keep raws and just regenerate the locus layer for one project:
uv run loci rebuild transformer-attention
```

Use a **project** for intent and a **workspace** for source roots. If you
want MCP auto-resolution in the current directory, bind the project after
creating it:

```bash
uv run loci project bind transformer-attention
```

## Layout

```
src/loci/
  config.py         # settings + paths
  db/               # schema, migrations, connection (sqlite + sqlite-vec)
  embed/            # local embedding model
  graph/            # node/edge/project repositories
  ingest/           # walk → hash → dedup → extract → embed
  retrieve/         # lex + vec + hyde + PPR
  citations/        # trace + response writers
  jobs/             # background queue + absorb pipeline
  llm/              # anthropic client + prompt assembly
  api/              # FastAPI REST + WebSocket
  mcp/              # MCP adapter (curated subset of REST)
  cli.py            # typer CLI
```

## License

MIT.
