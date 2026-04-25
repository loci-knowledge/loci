# The interpretation graph

This document explains what the graph is, what each node and edge type means,
how they come into existence, and how to read and manipulate the graph over
time.

## Overview

Every loci project owns a three-level structure:

```
raw nodes  (files)
    ↓  cites
interpretation nodes  (distillations)
    ↓  reinforces / extends / co_occurs / …
interpretation nodes  (meta-level)
```

The **raw layer** is your source material — PDFs, code files, notes,
transcripts. Each file is one raw node. The raw layer is read-only from the
agent's perspective; only ingestion (scan) writes here.

The **interpretation layer** is the agent's (and your) thinking about that
material. It is the living, evolvable part of the graph. Interpretation nodes
start conservative (confidence 0.40) and get reinforced, softened, linked, and
occasionally dismissed as you work.

The two layers are connected by `cites` edges — an interpretation node that
draws on a raw source as evidence points to it. This is how the graph stays
grounded.

---

## Node types

### Raw nodes

| field          | meaning                                                   |
|----------------|-----------------------------------------------------------|
| `kind`         | always `"raw"`                                            |
| `subkind`      | `pdf \| md \| code \| html \| transcript \| txt \| image` |
| `title`        | filename without extension (or extracted heading for PDFs) |
| `body`         | extracted plain text — what FTS5 and the embedder see     |
| `content_hash` | sha256[:16] of file bytes — the deduplication key         |
| `canonical_path` | absolute path on disk at ingest time                    |
| `source_of_truth` | `false` if the file has since moved or been deleted    |
| `confidence`   | always 0.5 — not used for ranking raw nodes               |
| `status`       | `live` normally; `dirty` if file is missing               |

Raw nodes are **content-addressed**: the same PDF scanned from two workspaces
gets one raw node (one embedding, one FTS row) with two workspace membership
entries. Editing the file on disk does not change the raw node until you
re-scan — the existing hash still matches the old bytes.

**How a raw node is created:** `loci workspace scan <ws>` walks each
registered source root. For each file: sha256 hash → look up in `raw_nodes`
by content hash. If absent: extract text, batch-embed, write `nodes` +
`raw_nodes` + `node_vec` + FTS row + `workspace_membership`. If present: write
`workspace_membership` only (dedup — no re-embedding).

---

### Interpretation nodes

Every interpretation node has:

| field          | meaning                                                   |
|----------------|-----------------------------------------------------------|
| `kind`         | always `"interpretation"`                                 |
| `subkind`      | one of the 7 types below                                  |
| `title`        | ≤80 chars — the claim or question in brief                |
| `body`         | 1–4 sentences — the full statement                        |
| `confidence`   | 0.0–1.0 — how established the interpretation is           |
| `status`       | `live → dirty → stale → dismissed`                       |
| `origin`       | `agent_synthesis \| user_explicit_create`                 |
| `angle`        | (relevance only) see below                                |
| `rationale_md` | (relevance only) the "because" clause                     |

#### Subkinds

**`question`** — An open question the project must answer, not yet resolved.
Kickoff produces these. They seed retrieval immediately and invite, rather than
assert. Low confidence by default (0.5). As you draft answers, a
`question`→`pattern` arc can emerge: the question gets softened, a new pattern
node reinforces next to it.

*Created by:* kickoff job + reflect cycle.

---

**`pattern`** — A recurring structure or approach that appears across sources.
Not a summary of one source — it must manifest in multiple places. The body
names the trigger/cycle/outcome, not the paper it appears in.

*Example:* "analysis before presentation — infer structure first, build the
output surface after."

*Created by:* reflect cycle (when candidates across multiple sources share a
structural similarity).

---

**`decision`** — A concrete choice the project has made (or must make), with
the trade-offs named. Decisions are useful precisely because they record *why*
a path was chosen — the alternatives that were discarded, the constraint that
forced the choice.

*Example:* "keep the repo as source of truth — DeepWiki wraps, not replaces."

*Created by:* reflect cycle (when the draft grapples with a fork in approach).

---

**`tension`** — Two values or requirements that genuinely pull against each
other in a way that doesn't resolve. Not a problem to fix — a creative
constraint to navigate. A tension is honest about the cost of either side.

*Example:* "transparency should be there, but not in the way — provenance
on-demand, not by default."

*Created by:* reflect cycle (when cited candidates embody competing
commitments). Also from absorb's `detect_broken_supports` (a citation chain
breaks, signalling instability in an assumed resolution).

---

**`philosophy`** — A first-principle belief that grounds the project's
direction. The most durable and least actionable node type. A philosophy
should be something you'd invoke to settle a dispute about priorities, not a
description of an approach.

*Example:* "navigation should start from the code's shape, then add meaning."

*Created by:* reflect cycle (rarely — only when the agent observes a
consistent axiom, not a pattern or decision).

---

**`question` (kickoff variant)** — Same type, but kickoff questions are
written at confidence 0.50 (slightly above agent-synthesised patterns/decisions
at 0.40) because they're meant to surface immediately in retrieval. Questions
written by the reflect cycle are also at 0.40.

---

**`relevance`** — A typed bridge between one or more information workspaces
and the project's intent, at a named angle. This is the multi-source
connective tissue: it explains *how* a codebase, paper set, or notes collection
serves what the project is building — not what the sources say, but the bridge.

Required fields:

- `angle` (required) — one of:

  | angle                    | use when                                               |
  |--------------------------|--------------------------------------------------------|
  | `applicable_pattern`     | a structural approach in source X maps to what you're building |
  | `experimental_setup`     | source X's study design is directly reusable           |
  | `borrowed_concept`       | a term/framework from source X is being adopted        |
  | `counterexample`         | source X shows a case that bounds or challenges your claim |
  | `prior_attempt`          | source X tried something similar; note what worked/failed |
  | `vocabulary_source`      | the project is adopting source X's naming              |
  | `methodological_neighbor`| source X uses a similar method in a different domain   |
  | `contrast_baseline`      | source X is the thing you're distinguishing yourself from |

- `rationale_md` (required) — 1–3 sentences, the "because" clause. What
  specifically makes these raws relevant at this angle? Do not summarise the
  sources; name the bridge.

A relevance node cites ≥2 raws (ideally from different workspaces). Its body
is 2–4 sentences explaining the bridge. Think: "the reason these sources matter
for this project is X — and the useful thing they show is Y."

*Created by:* reflect cycle (when candidates span multiple workspaces and a
clear bridge to the project profile exists) and by the `relevance` job
(triggered on workspace link or incremental scan).

*Refined by:* `update_angle` action (the agent or user refines the angle or
rationale without recreating the node).

---

## Edge types

Edges are stored in the `edges` table with `src`, `dst`, `type`, `weight`.
Some types are symmetric (auto-create their reciprocal); `specializes`
auto-creates an inverse `generalizes`.

| type             | direction     | symmetric | meaning                                                   |
|------------------|---------------|-----------|-----------------------------------------------------------|
| `cites`          | interp → raw  | no        | The interpretation draws on this raw as evidence. The primary structural link between layers. |
| `reinforces`     | interp → interp | yes      | Two interpretations support each other — seeing one makes the other more credible. |
| `co_occurs`      | interp → interp | yes      | Two interpretations cite the same raw node (shared evidence). Created automatically by absorb and kickoff. |
| `extends`        | interp → interp | no       | One interpretation elaborates or specialises another without contradiction. |
| `specializes`    | interp → interp | no       | A focused version of a more general interpretation. Auto-creates inverse `generalizes`. |
| `generalizes`    | interp → interp | no       | Inverse of `specializes` (auto-created).                 |
| `contradicts`    | interp → interp | yes      | Two interpretations claim incompatible things. Created by the contradiction pass in absorb. |
| `aliases`        | interp → interp | yes      | Two interpretations are effectively the same claim at different abstraction levels. Created by absorb's alias detection (cosine > 0.92). |

### How edges are created

| source            | edge types produced                                      |
|-------------------|----------------------------------------------------------|
| Reflect cycle     | `cites` (from new interp to cited raws), `reinforces`, `extends`, `specializes`, `generalizes` (from `link` actions) |
| Kickoff post-write | `cites` (anchor wiring — nearest raws by cosine), `co_occurs` (co-citation pairs) |
| Absorb step 9     | `co_occurs` (idempotent co-citation update)              |
| Absorb contradiction pass | `contradicts`, `reinforces` (from LLM classifier) |
| Absorb alias detection | `aliases` (from cosine > 0.92 threshold)          |
| User action       | any type via `POST /edges` or `loci link`                |

**Anchor wiring** (automatic): when a new interpretation node has no `cites`
edge yet (isolated), kickoff computes its cosine similarity against all raw
nodes and wires the 3 nearest with `weight = cosine_sim`. This ensures every
interpretation is grounded from minute one.

**Co-citation** (automatic): two interpretation nodes that both cite raw R get
a `co_occurs` edge. This edge means "shared evidence" — they're bound by the
same source material, even if their claims differ. It is the lightest and most
defensible structural edge: no inference required, just shared citation.

---

## Node lifecycle

```
               kickoff / reflect / user_create
                            │
                            ▼
                          [live]   ← confidence grows via reinforce
                         ↙    ↘
              [edit or         [support
              neighbor edit]    disappears]
                  │                │
               [dirty]          [stale]      ← interp citing deleted raws
                  │                │
         re-derive at          propose
         retrieve or absorb    broken-support
                  │
               [live]
                             explicit dismiss
              [any] ─────────────────────────→ [dismissed]  (terminal)
```

| status      | what it means                                               |
|-------------|-------------------------------------------------------------|
| `live`      | visible in retrieval, drafts, the graph; confidence tracked |
| `dirty`     | edited (own body or one-hop neighbor); re-derivation pending; shown in retrieval at reduced weight |
| `stale`     | all cited raws are gone or flagged broken; surfaced as a housekeeping proposal |
| `dismissed` | user removed it; permanently hidden; not deleted from DB    |

---

## Confidence signal

Confidence starts at 0.40 for agent-created nodes (0.50 for kickoff questions)
and evolves through usage:

| event                    | Δ confidence |
|--------------------------|-------------|
| `reinforce` action       | +0.05       |
| `soften` action          | −0.05       |
| `cited_kept` trace       | +0.02 (via next reflect cycle's reinforce action) |
| `cited_dropped` trace    | −0.03 (via next reflect cycle's soften action)   |
| `ACCEPT_EXPLICIT`        | +0.15       |
| `PIN`                    | role → pinned; used as PPR anchor; not a confidence delta |

User-created nodes typically start at 0.70 (the default in `POST /nodes`).
Pinned nodes typically sit at 1.0. Agent-written nodes take many reinforce
cycles to climb above 0.6, which means they rank below your own work in
retrieval until they've earned it.

Forgetting: absorb flags nodes with `confidence < FORGETTING_THRESHOLD` (default
0.20) and `last_accessed_at > FORGETTING_DAYS` (default 90) as dismissal
candidates. They don't auto-dismiss — they surface as proposals.

---

## How to read the graph

### The hierarchy

A well-developed loci graph has a natural gradient from *outer* (raw) to
*inner* (abstract):

```
raw sources  ──cites──▶  grounded interps  ──extends──▶  meta-interps
(files)                   (cite 1-2 raws)                (cite other interps)
                                ↕ co_occurs
                          (shared evidence)
```

Nodes closest to the raw layer (`cites` pointing outward) are the most
concrete. Nodes that only connect to other interpretation nodes are the most
abstract — they've been distilled enough that the original source material
isn't their primary claim.

The **radial layout** in the `/tmp/loci_graph.html` visualization encodes this:
- Center: philosophy + high-degree decision/pattern nodes
- Mid-ring: questions and relevance nodes
- Outer ring: raw source nodes

### The edge density gradient

A dense `co_occurs` cluster means several interpretations are drawing from the
same raw sources — this is your most fertile ground for synthesis. If 4
question nodes all co_occur via the same raw, ask: is there a pattern or
tension hiding here that should be written explicitly?

A `reinforces` chain means multiple interpretations support each other
independently — a sign of a stable belief. A `contradicts` edge is the inverse:
two beliefs in tension that the project hasn't resolved.

### Questions vs. claims

Questions (confidence 0.5) are placeholders. When the reflect cycle or a draft
produces a pattern or decision that answers a question, you can:
- Dismiss the question (it's been answered).
- Reinforce the question to keep it visible as a known-open thread.
- Create a `specializes` edge from the answer to the question (the answer is a
  specific instance of the broader question).

---

## Querying the graph directly

The SQLite database is at `loci.db` (dev) or `~/.loci/loci.sqlite` (default).

```sql
-- All live interpretation nodes, most accessed first
SELECT n.subkind, n.title, n.confidence, i.angle
FROM nodes n
JOIN interpretation_nodes i ON i.node_id = n.id
WHERE n.status = 'live'
ORDER BY n.confidence DESC, n.last_accessed_at DESC NULLS LAST;

-- Co-citation clusters: which raws anchor the most interpretation pairs
SELECT r.node_id, n.title AS raw_title, COUNT(*) AS pairs
FROM edges e1
JOIN edges e2 ON e1.dst = e2.dst AND e1.src < e2.src
JOIN nodes n ON n.id = e1.dst
JOIN raw_nodes r ON r.node_id = e1.dst
WHERE e1.type = 'cites' AND e2.type = 'cites'
GROUP BY r.node_id
ORDER BY pairs DESC;

-- Interpretation nodes with no raw support
SELECT n.id, n.subkind, n.title
FROM nodes n JOIN interpretation_nodes i ON i.node_id = n.id
WHERE n.id NOT IN (SELECT src FROM edges WHERE type = 'cites')
AND n.status = 'live';

-- Agent's deliberation for the last 5 reflection cycles
SELECT trigger, instruction, deliberation_md
FROM agent_reflections
ORDER BY ts DESC LIMIT 5;

-- What the agent did in the last cycle
SELECT ar.trigger, ar.instruction,
       json_each.value AS action
FROM agent_reflections ar,
     json_each(ar.actions_json)
ORDER BY ar.ts DESC
LIMIT 20;
```

---

## Graph construction pipeline (step by step)

This is the full lifecycle from file to graph structure.

### 1. Scan → raw nodes

```
loci workspace scan codoc-ws
  └─ walker: walk source roots, skip dotdirs / binaries / >50MB
  └─ for each path:
       sha256[:16]  →  look up raw_nodes.content_hash
       if exists:   write workspace_membership only  (dedup)
       else:
         extract_text(path)                (PDF → marker/pymupdf/pypdf)
         batch_embed(texts, model=bge-small-en-v1.5, dim=384)
         write nodes + raw_nodes + node_vec + FTS5 row
         write workspace_membership
```

### 2. Kickoff → first questions

```
loci kickoff codoc
  └─ sample 12 raws (most recent) + project profile
  └─ LLM: propose 8–10 open questions
  └─ write InterpretationNode (subkind=question, conf=0.5, origin=agent_synthesis)
  └─ anchor wiring: for each new question with no cites edge:
       cosine(question_emb, all_raw_embs) → top-3 nearest
       write cites edges (weight = cosine_sim)
  └─ co-citation: pairs of questions that cite same raw → co_occurs edges
```

### 3. Draft + reflect → interpretation graph

```
loci draft codoc "..."
  └─ Retriever: BM25 + vec ANN + PPR + RRF → top-k candidates
  └─ LLM (rag_model): write markdown with [Cn] citations
  └─ write Response + Trace rows
  └─ enqueue reflect job (non-blocking)

worker: reflect job
  └─ _build_context:
       project profile + pinned interps (voice anchor)
       task instruction + cited_node_ids
       retrieved-but-not-cited nodes (from traces)
       WORKSPACE CONTEXT (linked workspace names, kinds, sample raws)
       citation feedback (cited_kept/dropped/replaced) if any
  └─ SYNTHESISE (interpretation_model):
       → Action[] — create / reinforce / soften / link / update_angle
       subkind chosen from observed candidates:
         pattern: recurring structure across sources
         decision: a concrete choice with trade-offs
         tension: two competing values
         philosophy: a grounding axiom
         relevance: named bridge between workspace(s) and project intent
  └─ SELF-CRITIQUE (interpretation_model):
       → keep[] / drop[] — filter generic, duplicates, bad handles
  └─ APPLY surviving actions:
       create → write InterpretationNode (conf=0.40) + cites edges
       reinforce/soften → confidence ±0.05
       link → EdgeRepository.create (with symmetry/inverse)
       update_angle → set angle + rationale_md on existing relevance node
  └─ anchor wiring + co-citation (for newly created nodes)
```

### 4. Absorb → housekeeping

```
loci absorb codoc  (or POST /projects/:id/absorb)
  └─ fs_audit         : flip source_of_truth for missing files
  └─ replay_traces    : roll up traces → access_count, confidence
  └─ detect_orphans   : 0 edges → status=dirty
  └─ detect_broken_supports: dead raw → broken proposals, status=stale
  └─ detect_aliases   : cosine > 0.92 → alias proposals
  └─ detect_forgetting: low conf + no access → dismiss proposals
  └─ contradiction_pass: LLM 3-way classify each new raw vs top-3 interps
  └─ communities      : Leiden algorithm over interp graph
  └─ co_citation      : refresh co_occurs edges for all interp pairs
```

### 5. Workspace link → relevance pass

```
loci workspace link codoc-ws codoc (or POST /projects/:pid/workspaces/:wid)
  └─ insert project_workspaces row (sync)
  └─ enqueue relevance job (async)

worker: relevance job
  └─ sample workspace raws + project profile
  └─ focused single-pass synthesis (no critique stage)
  └─ write relevance InterpretationNode(s) with angle + rationale_md
  └─ write cites edges to supporting raws
  └─ update workspace_sources.last_scanned_at
```

---

## Visualization

The D3.js force graph at `/tmp/loci_graph.html` uses:

- **Radial force** — philosophy/high-degree nodes center, questions mid-ring,
  raws outer ring. This encodes the abstraction hierarchy spatially.
- **Node size** — proportional to degree + base size by subkind. High-degree
  nodes (heavily connected interpretations) appear larger.
- **Node color** by subkind:
  - question: blue
  - relevance: green
  - tension: red
  - philosophy: purple
  - pattern: yellow
  - decision: cyan
  - raw: grey
- **Edge style**:
  - `cites`: solid indigo
  - `reinforces`: solid green
  - `extends`: dashed amber
  - `co_occurs`: faint dashed (background structure — shared evidence, not semantic claim)
  - `specializes`/`generalizes`: dashed purple/blue
- **Click** any node for the side panel: full body, angle (if relevance),
  rationale, and the connection list.
- **Filter bar** (top center): toggle subkinds on/off to reduce visual noise.
- **Drag** nodes to re-pin them. **Scroll/pinch** to zoom. **Background click** closes the panel.

To regenerate the graph from the current DB state, use the CLI exporter:

```bash
uv run loci graph export codoc --output /tmp/loci_graph.html
```

That command reads the local loci database and writes a standalone HTML
snapshot. If your friend clones the repo, they will only see a graph after
they create or open a project with data and run the export command.

The co_occurs edges are shown at very low opacity so they don't obscure the
structural (`cites`, `reinforces`) edges. If you want to hide them entirely,
add `type != 'co_occurs'` to the edge export query.
