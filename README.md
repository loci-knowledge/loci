# loci

A personal memory graph server. Three layers — raw sources, an interpretation
graph, and per-project views — served to any client (Claude Code via MCP, a
VSCode extension, the CLI, plain HTTP) with a uniform citation contract.

See [`PLAN.md`](./PLAN.md) for the design.

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
uv run loci server          # start the HTTP/MCP server
uv run loci q transformer-attention "what is the rotary embedding insight?"
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
