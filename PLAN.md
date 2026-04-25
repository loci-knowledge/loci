# Loci — A Personal Memory Graph Server

## What loci is

Loci is a server that manages three layers of a user's intellectual history and serves them to any client that wants to do RAG, drafting, or graph manipulation against them. The central bet stands: generic RAG loses the user's personal *why*, and capturing that *why* as a first-class graph layer — shaped by feedback from the user's actual work — turns retrieval into something closer to recall.

The three layers, restated for a graph world:

1. **Memory space** — the raw substrate. PDFs, notes, codebases, web pages, transcripts. Content-addressed, mostly immutable.
2. **Interpretation graph** — typed nodes representing the user's distillations (philosophies, patterns, tensions, decisions, questions, touchstones, …) connected by typed edges to each other and to raw sources. **No tree, no canonical parent.** Hierarchical views are derived on demand (communities, clusters) and are not part of storage.
3. **Project layer** — every session is scoped to a project. A project is a *view* over the interpretation graph (a subgraph + a profile + a session log), not a separate graph. One paper or one note can participate in many projects without duplication.

## What changed from the previous draft

- **Tree → pure graph.** No `parent`. Spatial hierarchy was carrying the locus metaphor in the previous draft; the metaphor moves to the visualization layer (VSCode extension, force-directed or community-clustered) and out of storage.
- **Plugin → server.** Loci is a long-running process exposing MCP, HTTP/REST, and a CLI. Claude Code, the planned VSCode extension, and any other client (a future Obsidian plugin, a chat UI, a CLI script) are all peers. The server owns LLM-side jobs (absorb, contradiction detection, proposal generation) as background workers.
- **Citations are a server-level contract.** Every RAG response includes a `citations[]` block listing the nodes that contributed to it. Clients are expected to render these; the server provides expansion endpoints (`GET /nodes/:id`, `GET /nodes/:id/trace`) so a client can show "where did this come from."
- **Inspiration, not dependency.** The `context/` files (`wiki_gen_skill`, `llm_wiki`, `qmd`, `tolaria`) shaped the thinking but won't be referenced in the live system. What carries forward is the absorb/checkpoint discipline, the concept-article taxonomy, the local hybrid-search backend (BM25 + vec + hyde), and the typed-wikilink convention — re-implemented inside loci, not imported.
- **Use cases broadened.** The previous vignettes assumed short reading sessions. The real workload includes multi-turn drafting (literature review, implementation plans, proposal writing), augmentation ("rewrite this paragraph using the things I've read on X"), and code generation that must cite the sources/interpretations it drew from. The schema and API are designed around these.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Clients                                                        │
│  ┌─────────────┐  ┌──────────────┐  ┌──────┐  ┌──────────────┐  │
│  │ Claude Code │  │ VSCode ext.  │  │ CLI  │  │ Other (REST) │  │
│  │  (via MCP)  │  │  (graph UI)  │  │      │  │              │  │
│  └──────┬──────┘  └───────┬──────┘  └──┬───┘  └──────┬───────┘  │
└─────────┼─────────────────┼────────────┼─────────────┼──────────┘
          │ MCP             │ HTTP/WS    │ HTTP        │ HTTP
          ▼                 ▼            ▼             ▼
┌─────────────────────────────────────────────────────────────────┐
│  loci server (single process, local)                            │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  API surface: MCP adapter | REST | WebSocket            │    │
│  └────────────────────────┬────────────────────────────────┘    │
│  ┌────────────────────────┴────────────────────────────────┐    │
│  │  Core services                                          │    │
│  │   • Graph store (nodes, edges, projects, traces)        │    │
│  │   • Retrieval (lex + vec + hyde + Personalized PageRank)│    │
│  │   • Ingest (content-hash, dedup, extract, embed)        │    │
│  │   • Citation tracker (per-response provenance)          │    │
│  │   • Job queue (absorb, contradiction, proposals)        │    │
│  └────────────────────────┬────────────────────────────────┘    │
│  ┌────────────────────────┴────────────────────────────────┐    │
│  │  Storage                                                │    │
│  │   • SQLite (graph, projects, traces, jobs)              │    │
│  │   • Vector index (sqlite-vec or lancedb, embedded)      │    │
│  │   • Raw blobs on disk, content-addressed                │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

Three reasons this shape works:

- The **graph store is the source of truth** — markdown export is a derived view, not the substrate. Round-tripping through markdown was attractive in the previous draft but breaks down for graph queries (PPR, community detection, edge-typed traversals) at any non-trivial scale. Storing the graph in SQLite and exporting markdown views on demand is faster and cleaner.
- **The job queue is internal.** Absorb runs, contradiction detection, and proposal generation happen on a background worker the server owns. Clients fire-and-forget (`POST /projects/:id/absorb`) and poll or subscribe for completion. This matters because contradiction passes are LLM-heavy and we don't want a Claude Code session to block on them.
- **Citations are issued by the server, not assembled by the client.** When a client calls `POST /retrieve` or `POST /draft`, the response carries a `citations[]` array the client renders verbatim. This makes the citation contract the same across MCP, REST, and CLI.

## Data model

### Nodes

Two node kinds, sharing a base shape:

```
Node {
  id: string              # ULID, sortable by time
  kind: enum              # raw | interpretation
  subkind: string         # for raw: pdf | md | code | html | transcript
                          # for interpretation: philosophy | pattern | tension |
                          #                     decision | question | touchstone |
                          #                     experiment | metaphor
  title: string
  body: string            # markdown
  tags: string[]
  created_at, updated_at, last_accessed_at: timestamp
  access_count: int
  confidence: float       # [0,1], only meaningful for interpretation
  status: enum            # proposed | live | dirty | stale | dismissed
  embedding: vector       # computed; updated when body changes
}

RawNode extends Node {
  content_hash: string    # sha256(content)[:16], unique
  canonical_path: string
  mime: string
  size_bytes: int
  source_of_truth: bool   # false if file is missing/deleted
}

InterpretationNode extends Node {
  origin: enum            # user_correction | user_pin | user_summary |
                          #   user_explicit_create | proposal_accepted
  origin_session_id: string?
  origin_response_id: string?  # the response that triggered creation
}
```

A few notes on the schema:

- **No `parent`.** The graph is flat; structure comes from edges.
- **No `supports[]` / `related{...}` blobs in the node.** Edges live in their own table (below). The previous draft's frontmatter mixed node identity with relational structure; separating them lets the graph engine do its job.
- **`status` is a small state machine.** `proposed` (in the proposal queue) → `live` (accepted) → `dirty` (a neighbor changed; needs review) → `stale` (a support disappeared; held for the user to decide) → `dismissed` (soft-deleted, never re-proposed). One field, four well-defined transitions.
- **`embedding` is stored on the node.** Recomputed on body change; cheap and worth it.
- **`confidence` is only meaningful for interpretations.** Raw nodes either exist or don't; their `source_of_truth` flag covers their version of the same idea.

### Edges

Edges are typed and live in their own table:

```
Edge {
  id: string
  src: node_id            # always interpretation (or raw for `cites`)
  dst: node_id
  type: enum
  weight: float           # [0,1], used by PPR
  created_at: timestamp
  created_by: enum        # user | system | proposal_accepted
  symmetric: bool         # if true, (src,dst,type) implies (dst,src,type)
}
```

Edge types:

| Type           | Direction      | src kind        | dst kind       | Symmetric |
|----------------|----------------|-----------------|----------------|-----------|
| `cites`        | interp → raw   | interpretation  | raw            | no        |
| `reinforces`   | interp ↔ interp| interpretation  | interpretation | yes       |
| `contradicts`  | interp ↔ interp| interpretation  | interpretation | yes       |
| `extends`      | interp → interp| interpretation  | interpretation | no        |
| `specializes`  | interp → interp| interpretation  | interpretation | no (inverse: `generalizes`) |
| `aliases`      | interp ↔ interp| interpretation  | interpretation | yes       |
| `co_occurs`    | interp ↔ interp| interpretation  | interpretation | yes (system-derived) |

`cites` replaces what the previous draft called `supports`. The rename matters: `cites` is what the graph stores; *citation* (the user-facing concept of "this answer drew on these nodes") is computed at response time and includes both the interpretation nodes touched and the raw nodes they `cites` into.

`co_occurs` is system-derived from co-retrieval patterns and is the seed for community detection; it's never user-created.

### Projects

```
Project {
  id, slug, name
  profile_md: string      # the project profile (scope, goals, taste)
  created_at, last_active_at
  config: jsonb           # absorb cadence, retrieval defaults, etc.
}

ProjectMembership {
  project_id, node_id
  role: enum              # included | excluded | pinned
  added_at, added_by
}
```

A project is a *filter* over the global graph plus a profile. Adding a paper to a project doesn't move it; it asserts membership. Two projects sharing a paper share *that paper's node* — no duplication, no cross-project alias dance from the previous draft.

`role: pinned` means the node is a touchstone for this project — boosted in retrieval, surfaced in summaries.

### Traces and citations

```
Trace {
  id, project_id, session_id, response_id
  node_id
  kind: enum              # retrieved | cited | edited | accepted | rejected | pinned
  ts: timestamp
  client: string          # "claude-code" | "vscode" | "cli" | …
}

Response {
  id, project_id, session_id
  request: jsonb          # the original retrieve/draft request
  output: text
  cited_node_ids: string[]
  ts: timestamp
}
```

Every retrieve/draft call writes a `Response` row and a `Trace` row per cited node. This is what makes the citation-expansion endpoints (`GET /responses/:id`, `GET /nodes/:id/responses`) trivial: it's a join, not a reconstruction.

## API surface

The same operations are exposed through MCP, REST, and CLI. REST is the spec; MCP wraps a subset of it as tools; the CLI is a thin wrapper over REST.

### Ingest and project setup

```
POST /projects                       # create
POST /projects/:id/sources           # register a path or URL; returns content_hash
POST /projects/:id/sources/scan      # walk a directory, dedup, embed
GET  /projects/:id                   # profile, member count, last absorb, etc.
PATCH /projects/:id/profile          # update project.md
```

### Retrieval

```
POST /projects/:id/retrieve
  body: { query, k, anchors?: node_id[], include?: kinds[], hyde?: bool }
  returns: {
    nodes: [{ id, kind, subkind, title, snippet, score, why }],
    citations: [{ node_id, contributing_score, edges_traversed }],
    trace_id
  }
```

`anchors` lets the caller seed Personalized PageRank with the current task's nodes (HippoRAG 2 pattern). If omitted, the server falls back to the project's pinned nodes plus the top-k vector hits as anchors. `why` is a short string ("matched on rotary embeddings; reinforces position-in-projection") generated from the edges traversed — cheap, not LLM-generated, derived from edge types and weights.

### Drafting (the operation Claude Code will hit most)

```
POST /projects/:id/draft
  body: {
    instruction,        # "augment this paragraph with what I've read on X"
                        # "draft an implementation plan citing prior work"
                        # "summarize the cross-attention paper and link to my notes"
    context_md?,        # optional: the user's draft so far
    anchors?: node_id[],
    style?: "prose" | "outline" | "code-comments" | "bibtex",
    cite_density?: "low" | "normal" | "high"
  }
  returns: {
    output_md,
    citations: [
      {
        node_id, kind, subkind, title,
        why_cited,                 # one-liner: "supports the claim about ..."
        raw_supports: node_id[]    # if interpretation, the raw nodes it cites
      }
    ],
    response_id
  }
```

This is where the citation contract pays off. The server runs retrieve → assemble context → generate (using whatever LLM the server is configured with; default is the user's Anthropic key) → return both the output and the citation list. Per-response granularity means we don't promise sentence-level attribution we can't reliably deliver, but we do promise the client can render "this draft drew on [these nodes], expand any of them."

### Citation expansion

```
GET /responses/:id                   # full response + citations
GET /nodes/:id                       # node + edges + raw supports
GET /nodes/:id/trace                 # session history of this node
GET /nodes/:id/responses             # responses that cited this node
```

The VSCode extension hits these for hover-cards and the graph view. Claude Code can fetch them on demand when the user asks "where did this come from?".

### Graph manipulation

```
POST /nodes                          # create (usually proposed status)
PATCH /nodes/:id                     # edit body / title / tags / status
POST /edges                          # create
DELETE /edges/:id
POST /nodes/:id/accept               # proposed → live
POST /nodes/:id/dismiss              # → dismissed
POST /nodes/:id/pin                  # role: pinned in current project
GET  /projects/:id/proposals         # the proposal queue
```

VSCode-flavored:

```
GET  /projects/:id/graph             # nodes + edges, with layout hints
WS   /projects/:id/subscribe         # push graph deltas to the extension
```

### Background jobs

```
POST /projects/:id/absorb            # enqueue a checkpoint
GET  /jobs/:id                       # status: queued | running | done | failed
WS   /jobs/:id/subscribe             # progress updates
```

Absorb runs the contradiction pass, rebuilds the proposal queue, replays traces into `access_count` / `last_accessed_at` / `confidence`, runs the orphan/broken-support/bloat audits, and re-runs community detection. It's expensive (multiple LLM calls); clients enqueue and don't block.

## The interaction → graph-update protocol

The previous draft's signal table holds up; reframing for the server:

| Signal                | Source                    | Server action                                                                |
|-----------------------|---------------------------|------------------------------------------------------------------------------|
| `RETRIEVED`           | any retrieve/draft call   | `Trace(kind=retrieved)` per node returned                                    |
| `CITED`               | any draft call            | `Trace(kind=cited)`; `access_count++`; deferred confidence bump              |
| `ACCEPT_IMPLICIT`     | session ends without edit | confidence +0.05 on cited live nodes, deferred to absorb                     |
| `ACCEPT_EXPLICIT`     | `POST /nodes/:id/accept`  | confidence +0.15; status `proposed` → `live`                                 |
| `REJECT`              | `POST /nodes/:id/dismiss` | status → `dismissed`                                                         |
| `CORRECT_AND_CREATE`  | client detects correction | new interpretation node, conf 0.8, origin=`user_correction`, edges proposed  |
| `PIN`                 | `POST /nodes/:id/pin`     | role=pinned in project; touchstone subkind if newly created                  |
| `EDIT`                | `PATCH /nodes/:id`        | bump `updated_at`; mark one-hop neighbors `dirty`                            |
| `DECLARE_FLAW`        | client annotation         | confidence −0.2; status → `dirty`; queue review proposal                     |
| `INVOKE_ABSORB`       | `POST /projects/:id/absorb` | enqueue checkpoint job                                                     |

The bright line from the previous draft survives intact: only explicit gestures commit live nodes synchronously; everything else is traces, proposals, or deferred updates. What changes is that the *client* is responsible for detecting `CORRECT_AND_CREATE` (Claude Code can do this from conversation context; the VSCode extension exposes it as a button) and the *server* is responsible for everything downstream.

## Cost model (unchanged in shape, sharpened)

- **Per turn (synchronous, cheap):** retrieve runs lex + vec in SQLite (<100ms typical), PPR over a 10k-node graph adds maybe 50ms with a sparse implementation. Trace writes are single inserts. No LLM beyond the draft itself.
- **Per draft (synchronous, medium):** one LLM call; citation assembly is graph traversal, not LLM-mediated.
- **Per explicit gesture (synchronous, cheap):** one or two SQLite writes plus edge symmetry maintenance.
- **Per absorb (async, expensive):** batched LLM calls for contradiction detection (3-way classifier over (raw_added, top_3_interp_matches)), proposal generation, community detection (algorithmic, not LLM), audits. Run on a worker, surfaces deltas to subscribed clients.
- **Forgetting:** at absorb, interpretation nodes with `access_count == 0` over 30 days *and* `confidence < 0.3` become `dismissed` candidates — surfaced as a proposal, not auto-deleted.

## Vignettes, rewritten for the server

**1. Bootstrap a new project.** Client (CLI or VSCode) calls `POST /projects` with a slug and an initial profile, then `POST /projects/:id/sources/scan` pointing at `~/papers/transformer-attention/`. The server walks the directory, content-hashes each file, dedups against the global raw store, extracts text, embeds, and writes `RawNode`s. No interpretation nodes are created. The server enqueues a `kickoff` job that generates 5–10 `question` proposals from the profile + a sample of the raw — these land in the proposal queue, not the live graph.

**2. Augment a paragraph (the broader use case you flagged).** Claude Code, mid-draft, calls `POST /projects/:id/draft` with `instruction: "augment this paragraph with what I've read on rotary embeddings vs cross-attention"` and the user's current text in `context_md`. The server runs retrieve (anchored on nodes mentioned in the paragraph if any, else the project's pinned set), gets back ~8 nodes (mix of raw and interpretation), generates the augmented paragraph, returns it with `citations[]`. Claude Code renders the result; the user can hover any citation to expand it (the extension or terminal hits `GET /nodes/:id`). If the user accepts, no graph mutation; if they say "no, the real link is X," Claude Code detects `CORRECT_AND_CREATE` and posts a new interpretation node.

**3. Implementation plan with literature support.** User asks Claude Code to "draft an implementation plan for streaming Vega-Lite generation, citing prior work I've read." Claude Code calls `POST /projects/:id/draft` with `style: "outline"` and `cite_density: "high"`. The server retrieves heavily (k=20), generates a structured plan, and returns it with citations clustered per section. The client renders citations inline; expansion shows both the interpretation node ("live prompting compresses DSL into streamable tokens") and the raw papers it cites into. If the user later runs `POST /projects/:id/absorb`, the server's contradiction pass notices that one cited interpretation conflicts with a recent paper added since the last absorb, and files a `tension` proposal.

## Edge cases, updated for the graph world

1. **Raw deleted, interpretation cites it.** Audit pass flips the raw's `source_of_truth: false`; interpretation's effective citation count drops; if it falls below `min_supports`, status → `stale` and a `tension` proposal queues. Interpretations are never silently lost.
2. **Two interpretations look the same.** Cosine > 0.92 at absorb time → propose `aliases` edge. Aliases keep both in place (the user may have written them in different contexts and the difference matters); merging is a separate explicit gesture.
3. **User edits a node.** `dirty` propagates one hop on `cites`, `extends`, `specializes`, `reinforces`. Lazy: re-derivation happens when the dirty node is next retrieved or at absorb, whichever comes first.
4. **New raw contradicts existing interpretation.** Contradiction pass at absorb only. For each raw added since last absorb, retrieve top-3 interpretations, run a 3-way classifier (contradicts / reinforces / not-touch). Contradictions file `tension` proposals; reinforcements bump weight on existing `cites` edges; not-touch is dropped.
5. **Graph drift.** Absorb's audits: orphan (interpretation with 0 edges), broken-support (`cites` to non-`source_of_truth` raw), bloat (community with mean confidence < 0.3), thinning (community with < 3 nodes after pruning).
6. **Two projects share a paper.** Trivially handled: one `RawNode`, two `ProjectMembership` rows. No cross-project alias proposals needed; that complexity goes away.
7. **Cold start.** Day 1 graph has only `RawNode`s and `question` proposals. First ~20 accepted proposals calibrate the prior. The "no fabricated interpretations on day 1" rule from the previous draft holds.
8. **Implicit vs. explicit gestures.** Same bright line. Implicit signals (retrieved, cited without correction, time-on-node) update traces and confidence at absorb; explicit gestures (`accept`, `dismiss`, `pin`, `correct_and_create`) mutate live state synchronously.

## Open questions for implementation

These are the calls I'd punt to the build phase:

- **Embedding model.** Local (instructor-xl, bge-small) for offline-friendliness vs. remote (voyage, openai) for quality. The server should be configurable; defaults matter less than the seam; ANS: we should use local model, we can use the qmd too, it supports incremental embedding updates which is nice for the edit → dirty → re-embed flow.
- **PPR implementation.** scipy sparse PPR is fine to ~50k nodes; beyond that, switch to a precomputed top-k neighbor table per node refreshed at absorb. We don't need to make this call until users hit the limit.
- **Markdown export.** The server stores graph in SQLite, but a lot of users will want `git diff`-able markdown. Decision: provide `GET /projects/:id/export` that emits a markdown view (one file per node, frontmatter for edges) but treat it as one-way. Round-tripping is out of scope.
- **MCP tool granularity.** Should MCP expose every REST endpoint or a curated subset? Probably the latter: `loci.retrieve`, `loci.draft`, `loci.expand_citation`, `loci.propose_node`, `loci.accept_proposal`, and `loci.absorb`. Other ops are admin and don't need to be in the agent's tool list.
- **Multi-user / sync.** Out of scope for v1. Single-user, local-first. The architecture doesn't preclude a sync layer later but I'd rather see one user love it before designing for many.

## Inspiration carried forward (without dependency on context/)

For the build, the relevant ideas to absorb and re-implement:

- **Absorb / checkpoint discipline** with a fixed cadence and explicit audits.
- **Concept-article taxonomy** (`philosophy | pattern | tension | decision | question | touchstone | experiment | metaphor`) as the interpretation `subkind` enum.
- **Hybrid local search** (BM25 + vector + optional HyDE) as the retrieve substrate.
- **Typed wikilink edges** as the model for `Edge.type`.
- **Per-topic project profile** as the model for `Project.profile_md`.
- **Personalized PageRank seeded by task anchors** as the retrieve re-ranker.
- **Zettelkasten symmetric updates** (A-MEM): when an interpretation is created, candidate reciprocal edges on the touched neighbors are updated in the same transaction.
- **Event-triggered consolidation** (Mem0): no per-turn LLM consolidation.
- **Frequency × surprise × importance forgetting** (LUFY): the absorb-time prune candidates.
- **Episodic provenance** (when/where/which session): the `Trace` table.

The previous design treated these as files to reference; the new design treats them as informed defaults baked into the schema and the absorb job.