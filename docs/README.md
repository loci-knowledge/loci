# loci documentation

loci is a personal memory graph server. Three layers — raw sources, an
interpretation graph, and per-project views — served to any client (Claude
Code via MCP, the *Loki Town* VSCode extension, the CLI, plain HTTP) with a
uniform citation contract.

If you're reading docs for the first time, read in this order:

1. [getting-started.md](./getting-started.md) — install, first project,
   first scan, first kickoff, first draft, *and* connecting the VSCode
   extension. The whole flow end-to-end on a real example folder.
2. [frontend.md](./frontend.md) — the *Loki Town* VSCode extension that
   visualises the graph as a living town. Explains how the extension
   connects to a project, what each on-screen affordance does, and how to
   troubleshoot when the panel can't reach the server.
3. [agent.md](./agent.md) — the silent agentic pipeline that maintains the
   interpretation layer. **Read this early** — without it the rest is
   confusing because there is no proposal queue.
4. [architecture.md](./architecture.md) — how files flow through the system;
   what the interpretation graph is; how the reflect cycle evolves it.
5. [graph.md](./graph.md) — the graph reference: every node type, edge type,
   lifecycle state machine, confidence signal table, construction pipeline
   step-by-step, and how to read + query the graph.
6. [sources.md](./sources.md) — workspaces, source registration, supported
   formats, multi-root scanning, marker (high-quality PDF) setup.
7. [model-config.md](./model-config.md) — choosing which LLM provider/model
   is used for which task (interpretation, RAG, classifier, HyDE).
8. [session-lifecycle.md](./session-lifecycle.md) — the bigger picture: how
   a project evolves from "five questions" to a working knowledge graph.

The two repos:

- **`loci/`** (this repo) — the server: SQLite, embeddings, agent, REST,
  WS, MCP, CLI. Talks on `127.0.0.1:7077`. MCP tools support auto-resolution
  of the active project via `loci project bind <slug>` or `LOCI_PROJECT` env.
- **`loki-frontend/`** — the VSCode extension. Optional. Connects to the
  loci server.

The design spec is [`PLAN.md`](../PLAN.md) at the repo root. The docs explain
*how to use* what `PLAN.md` describes.
