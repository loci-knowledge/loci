# loci documentation

loci is a personal memory graph server. Three layers — raw sources, an
interpretation graph, and per-project views — served to any client (Claude
Code via MCP, the CLI, plain HTTP) with a uniform citation contract.

If you're reading docs for the first time, read in this order:

1. [getting-started.md](./getting-started.md) — install, first project, first
   scan, first kickoff, first draft.
2. [agent.md](./agent.md) — the silent agentic pipeline that maintains the
   interpretation layer. **Read this early** — without it the rest is
   confusing because there is no proposal queue.
3. [architecture.md](./architecture.md) — how files flow through the system;
   what the interpretation graph is; how the reflect cycle evolves it.
4. [sources.md](./sources.md) — file storage, supported formats, multi-root
   scanning, marker (high-quality PDF) setup.
5. [model-config.md](./model-config.md) — choosing which LLM provider/model is
   used for which task (interpretation, RAG, classifier, HyDE).
6. [session-lifecycle.md](./session-lifecycle.md) — the bigger picture: how a
   project evolves from "five questions" to a working knowledge graph.

The design spec is [`PLAN.md`](../PLAN.md) at the repo root. The docs explain
*how to use* what `PLAN.md` describes.
