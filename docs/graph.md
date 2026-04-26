# The loci-of-thought DAG

This document explains the graph: what it is, what each node and edge type
means, how nodes come into existence, and how to read the graph over time.

## The mental model

A loci graph is a **directed acyclic graph** in which:

- **Raw nodes are leaves.** They hold the actual content — PDFs, code files,
  notes, transcripts. Raws never have outgoing edges.
- **Interpretation nodes are inner nodes.** Each interpretation is a *locus
  of thought*: a pointer that says "the part of THIS source over here meets
  the part of THIS project over there, in this specific way." Loci are how
  retrieval finds its way back to the right paragraph of the right source.
- **Edges flow downward and inward**, never sideways or back. Two types:

  | type           | direction        | meaning                                                                  |
  |----------------|------------------|--------------------------------------------------------------------------|
  | `cites`        | interp → raw     | This locus points at this source. The grounding hop.                     |
  | `derives_from` | interp → interp  | This locus builds on / specialises / inherits routing from another locus. |

Both edge types are directed. There are no symmetric edges, no inverses, and
no raw→raw edges. `derives_from` insertions are rejected if they would close
a cycle.

```
                  ┌── derives_from ──┐
       interp A ─────────────────────┴──▶ interp B ─── derives_from ──▶ interp C
          │                                  │                                │
        cites                              cites                            cites
          ▼                                  ▼                                ▼
        raw R₁                             raw R₂                           raw R₃
```

A locus never holds the answer; the cited raw does. The locus tells you
which part of the raw matters and why for this project. Retrieval routes a
query through loci to surface the raws they point at — see
[architecture.md](architecture.md) for the algorithm.

---

## Node types

### Raw nodes

| field             | meaning                                                  |
|-------------------|----------------------------------------------------------|
| `kind`            | always `"raw"`                                           |
| `subkind`         | `pdf | md | code | html | transcript | txt | image`     |
| `title`           | filename (or extracted heading for PDFs)                 |
| `body`            | extracted plain text — what FTS5 and the embedder see    |
| `content_hash`    | sha256[:16] — the deduplication key                      |
| `canonical_path`  | absolute path on disk at ingest time                     |
| `source_of_truth` | `false` if the file has since moved or been deleted      |
| `confidence`      | always 1.0; not used for ranking raws                    |
| `status`          | `live` normally; `dirty` if file is missing              |

Raws are content-addressed: the same PDF in two workspaces gets one raw node
with two `workspace_membership` rows. Editing the file on disk does not
change the raw until you re-scan.

### Interpretation nodes — the three slots

Every interpretation has three required content slots that distinguish a
locus from a summary:

| slot               | meaning                                                                                |
|--------------------|----------------------------------------------------------------------------------------|
| `relation_md`      | 1–3 sentences. How does the source(s) relate to *this project*? Concrete bridge.       |
| `overlap_md`       | 1–2 sentences. WHERE do they intersect? Specific.                                      |
| `source_anchor_md` | Which PART of which source carries the weight? Quote, section, function, line range.  |

The legacy `body` field is optional and only used for additional free-form
context. The three slots are what retrieval and the LLM read to understand
why a locus routes to a raw.

Interpretations also carry:

| field                | meaning                                                  |
|----------------------|----------------------------------------------------------|
| `subkind`            | one of `philosophy | tension | decision | relevance`     |
| `title`              | ≤80 chars — the locus name (NEVER ends in `?`)           |
| `confidence`         | 0.0–1.0 — how established this locus is                 |
| `status`             | `live → dirty → stale → dismissed`                      |
| `origin`             | `agent_synthesis | user_explicit_create | proposal_accepted | …` |
| `angle`              | (relevance only) closed vocabulary — see below           |

#### Subkinds (the framing of a locus)

- **`relevance`** — typed bridge across distinct sources. Required `angle` from:
  `applicable_pattern`, `experimental_setup`, `borrowed_concept`,
  `counterexample`, `prior_attempt`, `vocabulary_source`,
  `methodological_neighbor`, `contrast_baseline`. Cite ≥2 raws.
- **`philosophy`** — first-principle belief the sources reveal the project
  should hold. The `relation_md` says how it shows up; the `source_anchor_md`
  points at where it is stated/embodied.
- **`tension`** — an unresolved conflict between source(s) and project, or
  between two values the project must reconcile. `source_anchor_md` points at
  where each side is visible.
- **`decision`** — a concrete choice with explicit trade-offs.
  `source_anchor_md` cites the evidence on each side.

---

## How loci are created

| trigger                                    | what happens                                                           |
|--------------------------------------------|------------------------------------------------------------------------|
| `loci kickoff <project>`                   | Generate first set of loci over the project's profile + workspace samples. |
| Reflect cycle (post-draft / -feedback)     | Synthesise + critique → create / reinforce / soften / link.            |
| User: `POST /nodes` or `loci_propose_node` | Direct user-authored locus.                                            |

When a locus is written, its three slots are also concatenated for embedding
(so vec retrieval scores the locus's *bridge*, not just its title). Cites
edges to anchored raws are added in the same step. `derives_from` edges to
upstream loci are added when the agent or user explicitly relates one locus
to another.

If a brand-new locus has no `cites` edges (an orphan), kickoff's anchor pass
attaches it to its top-3 nearest raws by cosine similarity so it isn't
floating.

---

## Lifecycle

```
                  kickoff / reflect / user_create
                              │
                              ▼
                            [live]   ← confidence grows via reinforce
                          ↙        ↘
              [edit or               [support
              neighbor edit]         disappears]
                  │                       │
              [dirty]                  [stale]    ← locus citing missing raws
                  │                       │
       re-derive at retrieve         broken-support
       or absorb                     proposal
                  │
              [live]
                              explicit dismiss
              [any] ─────────────────────────────▶ [dismissed]  (terminal)
```

Confidence starts at 0.40 for agent-created loci (0.50 for kickoff) and
evolves through `reinforce`/`soften` actions and absorb's trace replay.

---

## How retrieval uses the DAG

Retrieval is **interpretation-routed**:

1. Score loci against the query (lex + vec + HyDE + PPR over `derives_from`).
2. Walk `cites` and `derives_from·cites` from the top loci to raws.
3. Also score raws directly against the query.
4. Merge: direct raw score + (capped) routing bonus from loci.
5. Return raws + a per-raw trace through the locus DAG.

**The raws are the answer.** The loci are the routing context — they appear
in a side panel (`routing_loci`) and in a per-raw `trace_table`, never as
citable content. See [architecture.md](architecture.md#retrieval) for the
full pipeline.

---

## Querying the graph directly

```sql
-- All live loci by confidence (with their three slots)
SELECT n.subkind, n.title, n.confidence,
       i.relation_md, i.overlap_md, i.source_anchor_md, i.angle
FROM nodes n
JOIN interpretation_nodes i ON i.node_id = n.id
WHERE n.status = 'live'
ORDER BY n.confidence DESC, n.last_accessed_at DESC NULLS LAST;

-- Raws that are reached by the most loci (high-routing raws)
SELECT n.title, COUNT(*) AS pointed_at_by
FROM edges e
JOIN nodes n ON n.id = e.dst
WHERE e.type = 'cites'
GROUP BY n.id
ORDER BY pointed_at_by DESC;

-- The derives_from DAG (locus → upstream locus)
SELECT a.title AS derived_locus, b.title AS upstream_locus
FROM edges e
JOIN nodes a ON a.id = e.src
JOIN nodes b ON b.id = e.dst
WHERE e.type = 'derives_from';

-- Orphan loci — no cites edge yet
SELECT n.id, n.subkind, n.title
FROM nodes n
JOIN interpretation_nodes i ON i.node_id = n.id
WHERE n.id NOT IN (SELECT src FROM edges WHERE type = 'cites')
  AND n.status = 'live';

-- Last 5 reflection cycles
SELECT trigger, instruction, deliberation_md
FROM agent_reflections ORDER BY ts DESC LIMIT 5;
```

---

## Visualisation

The D3 force graph at `/tmp/loci_graph.html` (run `loci graph export
<project>`) uses:

- **Node colour by subkind**: tension (red), decision (yellow), philosophy
  (purple), relevance (cyan), raw (grey).
- **Edges**:
  - `cites` (dashed grey) — interp → raw, the grounding pointer.
  - `derives_from` (solid purple) — interp → interp, the inheritance.
- **Click** any node for the side panel showing the three locus slots
  (relation / overlap / source anchor) plus connections.
- **Drag** to pin, **scroll** to zoom.

The graph has no cycles. The deepest interpretations sit furthest from raws
(several `derives_from` hops up); the most concrete loci sit one `cites`
edge above their raws.

---

## Resetting

When the schema or prompts change, the cleanest path is a wipe:

```bash
loci reset                 # confirms, then drops loci.db + blobs
loci workspace create …    # set up sources
loci workspace add-source …
loci workspace scan <ws>
loci project create <slug>
loci workspace link <ws> <project>
loci kickoff <project>
```

Or to rebuild a single project's loci layer (raws preserved):

```bash
loci rebuild <project>
```
