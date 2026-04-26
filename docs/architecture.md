# Architecture

The mental model in one sentence: **loci is a single-process server that owns
a SQLite database and an embedding model, and serves a uniform citation
contract over MCP, REST, and a CLI.**

## The three layers, restated

PLAN §What loci is defines three layers. They are *all stored in SQLite*
(not on disk in your vault); markdown export is a derived view, not the
substrate.

### 1. Memory space — `RawNode`s

Every file you ingest becomes one `RawNode`:

| field             | meaning                                        |
|-------------------|------------------------------------------------|
| `content_hash`    | sha256[:16] of the file bytes — dedup key      |
| `canonical_path`  | absolute path on disk                          |
| `mime`, `size_bytes`, `subkind` (`pdf|md|code|html|transcript|txt|image`) | |
| `body`            | extracted plain text — what FTS5 + the embedder see |
| `source_of_truth` | `false` if the file at `canonical_path` is missing  |

The original file bytes are also stored under `~/.loci/blobs/<hash[:2]>/<hash[2:]>` — content-addressed, deduped. This lets us re-extract later if we
swap to a better PDF parser without re-reading the source file.

### 1b. Information Workspaces

An information workspace is a **named bag of source roots** that can be
linked to one or more projects. Workspaces decouple *where you store files*
from *which projects care about them*: a PDF library scanned once is
available to every project that links its workspace, without re-embedding.

| table | key columns | meaning |
|---|---|---|
| `information_workspaces` | `id`, `slug`, `name`, `description_md`, `kind` | Named source collection with an optional human-readable description |
| `workspace_sources` | `id`, `workspace_id`, `root_path`, `label` | Source roots owned by a workspace; scan walks these paths |
| `workspace_membership` | `workspace_id`, `node_id` | Which raw nodes belong to which workspace (populated by scan) |
| `project_workspaces` | `project_id`, `workspace_id`, `role`, `weight` | M:N link; `role` ∈ `primary` / `reference` / `excluded` |

The `project_effective_members` view is derived automatically:

```sql
-- pseudocode
(project_workspaces ⋈ workspace_membership WHERE role != 'excluded')
  ∪ project_membership(role = 'included')
  ∖ project_membership(role = 'excluded')
```

This means:
- All raws reachable through a non-excluded workspace link are visible to
  the project.
- Explicit `project_membership` rows (pins, manual includes) layer on top
  as overrides.
- Explicit `excluded` membership rows act as a veto regardless of workspace
  linkage.

Projects no longer own source roots directly; source roots belong to
workspaces. One workspace can serve N projects; a raw node scanned into a
workspace becomes available to all linked projects without re-embedding.

### 2. Interpretation graph — `InterpretationNode`s + `Edge`s

Your distillations. Four subkinds:

- `philosophy` — first-principle belief that grounds the project's direction
- `tension` — open question or unresolved conflict (kickoff writes these; they assert nothing and invite reasoning; confidence 0.5)
- `decision` — concrete choice with named trade-offs
- `relevance` — typed bridge between workspace(s) and the project's intent; always multi-source (cites ≥2 raws), requires `angle`

Edges are typed: `cites` (interp→raw, grounds interpretations in sources),
`semantic` (interp↔interp, symmetric — meaning-based relationships created by
the reflect cycle and absorb passes), `actual` (raw↔raw, explicit dependencies
like code imports or paper citations). `cites` edges are what become the
`raw_supports[]` block in citations.

A node moves through a small state machine:

    proposed → live          via accept gesture
    live     → dirty         via edit (own, or one-hop neighbor)
    dirty    → live          via re-derivation at retrieve / absorb
    live     → stale         via support disappearance (audit)
    *        → dismissed     via explicit dismiss (terminal)

### 3. Project layer — `Project` + `ProjectMembership`

A project is a *filter* over the global graph plus a profile. Adding a paper
to a project doesn't move it; it asserts membership. Two projects sharing a
paper share *that paper's node* — no duplication.

`role`:
- `included` — visible to retrieve/draft for this project (default).
- `excluded` — explicitly hidden from this project.
- `pinned`   — touchstone; boosted in retrieval, used as a default PPR anchor.

The effective member set for a project is computed by
`project_effective_members` (see "Information Workspaces" above). Explicit
`project_membership` rows take precedence over workspace-derived membership.

## Storage layout on disk

```
~/.loci/
  loci.sqlite               # the graph, FTS5 index, vec0 index, jobs, traces
  loci.sqlite-wal           # WAL journal
  blobs/                    # content-addressed raw file bytes
    a1/b2c3...              #   <hash[:2]>/<hash[2:]>
    ...
  models/                   # sentence-transformers cache
    BAAI--bge-small-en-v1.5/
```

Override the data dir with `LOCI_DATA_DIR=/path`.

The SQLite database holds *everything graph-related* — nodes, edges,
projects, memberships, workspaces, FTS5 inverted index, vector index, jobs,
traces, responses, proposals, communities. Backing up loci is `cp loci.sqlite*
backup/`. Moving to a new machine is `rsync ~/.loci/`.

## Request flow

### A retrieve call

```
POST /projects/:id/retrieve { query, k, anchors?, hyde? }
   │
   ▼
loci.retrieve.Retriever
   ├─ lex.search()          BM25 over nodes_fts
   ├─ vec.search_text()     embed(query) → ANN over node_vec
   ├─ hyde.hypothesize()    LLM(query) → embed → ANN  (only if hyde=True)
   ├─ ppr.run(anchors)      sparse PPR over interp graph
   │      anchors = caller-supplied OR pinned ∪ top-k vec hits
   └─ RRF fusion (k=60)     reciprocal rank fusion of all four channels
   │
   ▼
materialise nodes (incl. snippets) →
update access_count, last_accessed_at →
write Response + per-node Trace rows →
enqueue lightweight reflect (if MCP, with 5-min cooldown) →
return { nodes[], citations[], trace_id }
```

`why` strings are derived per-node from the channels that matched —
no extra LLM call.

### Awareness endpoints

Two endpoints expose the graph's live state for clients (VSCode extension, MCP):

```
GET /projects/:id/context
   → project info + linked workspaces (with raw_count) + graph stats + last 10 accessed nodes

GET /projects/:id/recent-nodes?hours=24&kind=interpretation
   → nodes created or updated in the last N hours (max 168); kind filter optional
```

These are the primary feed for the frontend "what's active" panel and the
`loci_context` MCP tool. The VSCode extension subscribes to the WebSocket
`project:{id}` channel for push updates, and polls `recent-nodes` to show
what the agent has been adding or modifying.

### A draft call

```
POST /projects/:id/draft { instruction, context_md?, style, cite_density }
   │
   ▼
Retriever.retrieve()             # same pipeline as above, k=12 by default
   │
   ▼
build candidate block:
    [C1] kind=interpretation/pattern title="…" why-retrieved="…"
         <= 800 chars body
    [C2] kind=raw/md title="…" …
    …
   │
   ▼
pydantic-ai Agent (Settings.rag_model, instructions=SYSTEM_PROMPT cached)
   │  user msg: instruction + context_md? + candidate block + style/density hints
   │  output: markdown with [Cn] inline citations
   ▼
parse [Cn] markers → drop unknown handles (anti-fabrication) →
look up `cites→raw` for each cited interpretation (raw_supports[]) →
write Response + Traces (cited + retrieved) →
ENQUEUE reflect job (non-blocking — see "Reflection cycle" below) →
return { output_md, citations[], response_id }
```

### Reflection cycle (silent, agentic, after every draft)

This is the heart of the interpretation layer's evolution. See
[agent.md](./agent.md) for the full surface.

```
worker thread claims `reflect` job →
loci.agent.interpreter.reflect():
   1. _build_context:
      - fetch project profile + pinned interps (voice anchor)
      - fetch the draft response: instruction + cited_node_ids
      - fetch retrieved-but-not-cited nodes (from traces)
      - roll up citation feedback (cited_kept / cited_dropped / cited_replaced)
        if the user has submitted any
      - fetch linked workspaces (name, kind, description, 6 sample raw titles)
        and render WORKSPACE CONTEXT block for the synthesis prompt
   2. SYNTHESISE (interpretation_model):
      → propose Action[] = create | reinforce | soften | link | update_angle
      (subkind chosen from candidates actually observed, not defaulted to relevance)
   3. SELF-CRITIQUE (interpretation_model):
      → keep[] / drop[] indices + reason
   4. APPLY surviving actions:
      - creates: write InterpretationNode at conf 0.40, origin=agent_synthesis
      - reinforces / softens: confidence += 0.05 (signed)
      - links: edges via EdgeRepository.create (with symmetry/inverse)
      - update_angle: update angle + rationale_md on existing relevance node in place
   5. log to agent_reflections (deliberation_md + actions_json)
```

Trigger sources for the reflect job: `draft` (auto), `feedback`
(citation-level diff), `kickoff`, `manual`, `relevance` (workspace linkage
events — focused single-pass, no self-critique stage).

### Linkage events

Workspace linkage and scan events generate both synchronous and asynchronous
effects:

| Event | Sync | Async |
|---|---|---|
| Link W→P | insert `project_workspaces` join row | enqueue `relevance(scope=link)` |
| Unlink W from P | remove `project_workspaces` join row | enqueue `sweep_orphans` — finds live interpretation nodes whose all cited raws are no longer in `project_effective_members`, flips them to `dirty`, files `forget` proposals |
| Workspace gains raw (during scan) | insert `workspace_membership` | for each linked project where role != `excluded`: enqueue `relevance(scope=incremental)`, deduped via fingerprint |
| Profile change | update project profile | optional `relevance(scope=profile_refresh)`, gated by config |

### A scan call

```
POST /projects/:id/sources/scan-all
   │
   ▼
For each registered source root:
    walk(root)                      # filtered by ext, skip dotdirs/binaries
    for each path:
        sha256 → trunc[:16]
        if hash already in raw_nodes: add membership only (dedup)
        else: extract → batch with siblings
    flush_batch:
        embedder.encode_batch(batch)  # one model call per N files
        for each: store_blob, INSERT raw_node + membership + node_vec
   ▼
return summary { scanned, new_raw, deduped, skipped, members_added, errors }
```

### An absorb call (background, housekeeping only)

After Phase F's silent agentic pivot, absorb is a periodic *housekeeping*
pass — the per-draft reflect cycle handles the work that absorb used to do
in batch (creating + reinforcing interpretations from new evidence).
Absorb still runs the slow, graph-wide checks:

```
POST /projects/:id/absorb        # enqueue
worker thread picks it up and runs jobs.absorb.run():
   1. fs_audit             : flip source_of_truth for missing/restored raws
   2. replay_traces        : roll up remaining cited traces → access_count, confidence
   3. detect_orphans       : interp nodes with 0 edges → status='dirty'
   4. detect_broken_supports: interps citing dead raws → 'broken' proposals; mark 'stale' if all gone
   5. detect_aliases       : cosine > 0.92 between interps → 'alias' proposals
   6. detect_forgetting    : low conf + no access N days → dismiss proposals
   7. contradiction_pass   : (LLM) for each new raw, classify against top-3 interps; tensions / reinforces
   8. communities          : (igraph) Leiden over the interp graph
   9. co_citation_edges    : find pairs of interpretation nodes that both cite the same raw
                             (via existing `cites` edges); create `co_occurs` edges between them.
                             Safe to re-run — idempotent. Also runs in kickoff as a post-write step.
```

The proposals from absorb are the *only* surface where the user is asked
to make explicit decisions: file moves, near-duplicate merges, stale
interps. Day-to-day interpretation construction happens silently via the
reflect cycle.

Every step records its summary into the job's `result` JSON. Each step is
idempotent and graceful: missing LLM key → step skips with a reason.

## How interpretations evolve

The interpretation graph isn't built up-front; it accretes from your work
through three stacked mechanisms:

1. **Per-draft reflection** (continuous, silent, agentic) — every `loci
   draft` triggers a background reflect cycle that may create / reinforce
   / soften interpretation nodes based on the task and the citations the
   draft chose.
2. **Citation-level feedback** (per user edit) — when you submit your
   edited markdown via `loci feedback`, we diff `[Cn]` markers and emit
   `cited_kept` / `cited_dropped` / `cited_replaced` traces. The next
   reflect cycle reads these and aligns accordingly.
3. **Absorb housekeeping** (periodic, batch) — the slow graph-wide
   checks (orphans, alias merges, forgetting, contradiction).

Updated signal table:

| signal                  | from                          | server action                       |
|-------------------------|-------------------------------|-------------------------------------|
| `RETRIEVED`             | any retrieve/draft call       | `Trace(kind=retrieved)` per node    |
| `CITED`                 | any draft call                | `Trace(kind=cited)`; access_count++ |
| `cited_kept`            | `loci feedback`               | trace; reinforces in next reflect   |
| `cited_dropped`         | `loci feedback`               | trace; softens in next reflect      |
| `cited_replaced`        | `loci feedback`               | trace; informs next reflect's synthesis |
| `requery`               | retrieve within window        | trace; signals previous answer was insufficient |
| `agent_synthesised`     | reflect cycle                 | new node at conf 0.40, origin=agent_synthesis |
| `agent_reinforced`      | reflect cycle                 | confidence +0.05                    |
| `agent_softened`        | reflect cycle                 | confidence −0.05                    |
| `ACCEPT_EXPLICIT`       | `POST /nodes/:id/accept`      | confidence +0.15 (rare; mostly housekeeping proposals) |
| `REJECT`                | `POST /nodes/:id/dismiss`     | status → dismissed                  |
| `CORRECT_AND_CREATE`    | client detects correction     | new interpretation node, conf 0.8   |
| `PIN`                   | `POST /nodes/:id/pin`         | role=pinned in project              |
| `EDIT`                  | `PATCH /nodes/:id`            | bump updated_at; one-hop dirty      |
| `INVOKE_ABSORB`         | `POST /projects/:id/absorb`   | enqueue housekeeping checkpoint     |

Bright lines:

- The agent **synthesises silently**. No proposal queue gates new nodes.
- The agent **never deletes**. Only `dismiss` (explicit user gesture) does.
- The user's **citation behaviour is the alignment signal**. Pin what
  matters, drop what didn't, and the layer aligns.

## What changes the model never does on its own

- It never fabricates interpretations on day 1 (kickoff produces *questions*,
  not statements).
- It never deletes a node — only the user's `dismiss` gesture does that.
- It never auto-merges two near-duplicate interpretations — it proposes the
  alias edge and waits.
- It never silently invents a citation in `loci draft` — handles that don't
  map to candidates are stripped.

Day-to-day interpretation construction is the silent reflect cycle (no
queue). The user-driven proposal queue is only used by absorb's
housekeeping suggestions — alias merges, broken-support flags, forgetting
candidates — never for new interpretations on the hot path.
