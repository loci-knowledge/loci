# loci documentation

loci is a personal memory graph server. Three layers — raw sources, an
interpretation graph, and per-project views — served to any client (Claude
Code via MCP, the *Loki Town* VSCode extension, the CLI, plain HTTP) with a
uniform citation contract.

If you're reading docs for the first time, read in this order:

1. [getting-started.md](./getting-started.md) — install, first project,
   first scan, first kickoff, first draft, *and* connecting the VSCode
   extension. The whole flow end-to-end on a real example folder.
2. [graph-ui.md](./graph-ui.md) — the **hosted D3 graph web UI** served
   directly by the loci server. Left-sidebar chat, trace-of-thought
   highlighting, clickable citations with verdict badges, and a live node
   editor. Start here for visual interaction.
3. [frontend.md](./frontend.md) — the *Loki Town* VSCode extension that
   visualises the graph as a living town. Explains how the extension
   connects to a project, what each on-screen affordance does, and how to
   troubleshoot when the panel can't reach the server.
4. [agent.md](./agent.md) — the silent agentic pipeline that maintains the
   interpretation layer. **Read this early** — without it the rest is
   confusing because there is no proposal queue.
5. [architecture.md](./architecture.md) — how files flow through the system;
   what the interpretation graph is; how the reflect cycle evolves it.
6. [graph.md](./graph.md) — the graph reference: every node type, edge type,
   lifecycle state machine, confidence signal table, construction pipeline
   step-by-step, and how to read + query the graph.
7. [sources.md](./sources.md) — workspaces, source registration, supported
   formats, multi-root scanning, marker (high-quality PDF) setup.
8. [model-config.md](./model-config.md) — choosing which LLM provider/model
   is used for which task (interpretation, RAG, classifier, HyDE).
9. [session-lifecycle.md](./session-lifecycle.md) — the bigger picture: how
   a project evolves from "five questions" to a working knowledge graph.
10. [research.md](./research.md) — the auto-research sub-agent: crawl papers
    (no HF account needed by default), optionally run code in an HF Spaces
    sandbox, and ingest findings into the graph. Covers live step-logging,
    `progress_display` polling, and the full MCP workflow.

The repos:

- **`loci/`** (this repo) — the server: SQLite, embeddings, agent, REST,
  WS, MCP, CLI, and the hosted graph web UI. Talks on `127.0.0.1:7077`. MCP
  tools support auto-resolution of the active project via `loci project bind
  <slug>` or `LOCI_PROJECT` env.
- **`loki-frontend/`** — the *Loki Town* VSCode extension. Optional. Connects
  to the loci server over the same HTTP/WS API as the built-in web UI.

The design spec is [`PLAN.md`](../PLAN.md) at the repo root. The docs explain
*how to use* what `PLAN.md` describes.
