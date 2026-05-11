# loci Aspect-Driven Retrieval: Current Design, Critique, and Redesign

A design note in the style of an ICLR technical report.

---

## §1 Current Pipeline: Formal Description

> **Note:** §1 documents the **v2.0** pipeline as it existed prior to the v2.1
> redesign. It is preserved verbatim so the critique in §2 has a concrete
> referent. For the live behaviour after the redesign, see §6 below and the
> retrieval block in `CLAUDE.md`.

### Notation

- $q$: the raw user query string
- $\mathcal{V}_p$: the aspect vocabulary for project $p$ — a set of `(id, label)` pairs from `aspect_vocab`
- $\varepsilon(q)$: the expanded aspect label set produced by `expand_query_aspects`
- $R_p$: the set of resources in project $p$ (via `project_effective_members`)
- $\mathcal{C}(r)$: the chunks belonging to resource $r$
- $h$: the HyDE hypothetical document string
- $\vec{h}$: the unit-normalised embedding of $h$ (BAAI/bge-small-en-v1.5, dim=384)

### 1.1 Query Expansion ε(q)

`concept_expand.py:40-112`

```
keywords(q) = { t | t ∈ tokenise(q), len(t) ≥ 2, t ∉ STOP_WORDS }

direct_matches = {
    (label, score) |
    label ∈ labels(V_p),
    ∃ kw ∈ keywords(q): partial_ratio(kw, label) ≥ 70
}   -- top-5 per keyword, via rapidfuzz

neighbor_labels = {
    label' |
    (label, _) ∈ direct_matches,
    r ∈ resources_for_aspect(label, p),
    r' ∈ co_aspect_neighbors(r, depth=1),
    label' ∈ aspects_for(r', p)
}

ε(q) = top_k( sorted(direct_matches, score DESC) ++ sorted(neighbor_labels, α) , k=5 )
```

The `embedder` parameter is accepted at the function signature but is unused; the comment at `concept_expand.py:44` confirms this.

### 1.2 Lexical Retrieval

`lex.py:35-124`

$$R_{\text{lex}} = \text{BM25-rank}\!\left(\{c \in \mathcal{C}(R_p) \mid \text{aspect}(c) \in \varepsilon(q)\},\; q\right)$$

BM25 column weights: `bm25(chunks_fts, 1.0, 0.4)` — text column 1.0, section column 0.4. The aspect filter is a hard JOIN: `resource_aspects.aspect_id IN aspect_vocab WHERE label IN ε(q)`. Scores are negative (SQLite FTS5 convention); the ordering is ascending (closest to zero = best). After fetching `chunk_k = max(limit*4, limit+20)` rows, results are deduplicated to one chunk per resource (top BM25 hit).

### 1.3 ANN Retrieval

`vec.py:46-130`

$$R_{\text{vec}} = \text{ANN-rank}\!\left(\{c \in \mathcal{C}(R_p) \mid \text{aspect}(c) \in \varepsilon(q)\},\; \vec{h}\right)$$

Distance metric: L2 over unit-normalised vectors. Because embeddings are unit-normalised, $d^2 = 2 - 2\cos\theta$, so smaller distance implies higher cosine similarity. The same hard JOIN aspect filter applies (`vec.py:74-79`). One chunk per resource retained (smallest distance = best span).

### 1.4 HyDE

`hyde.py:39-64`

$$h = \text{LLM}\!\left(q,\; \text{instructions} + \text{"Project context: " + } \texttt{profile\_md}[:500]\right)$$

`project_memo` is wired: `pipeline.py:167-170` fetches `projects.profile_md` and passes it. The expanded aspects $\varepsilon(q)$ are NOT passed to HyDE; the hypothetical document is grounded only in the project profile, not in the vocabulary match results.

### 1.5 RRF Fusion

`pipeline.py:209-250`

$$\text{rrf}(c) = \frac{1}{60 + \text{rank}_{\text{lex}}(c)} + \frac{1}{60 + \text{rank}_{\text{vec}}(c)}$$

where $\text{rank}_{\text{lex}}(c) = 0$ if $c \notin R_{\text{lex}}$ (term absent from lex results), and analogously for $\text{rank}_{\text{vec}}$. Lex results are already ordered ascending (most negative first = rank 1); vec results ordered ascending by distance (rank 1 = nearest). No score normalization is applied; raw lex/vec scores are stored in `ChunkResult` for provenance but RRF drives ordering. $k=60$ is the canonical default.

### 1.6 Graph Rerank

`pipeline.py:252-292`

Let $T = \text{top-5 unique resources by RRF}$ (the first 5 distinct resource IDs in sorted_chunks, `pipeline.py:256-264`).

For each $r \in T$, collect all edges where $r$ is src or dst, across edge types $E = \{$`co_aspect`, `cites`, `co_recalled`, `supports`, `instantiates`, `depends_on`, `contradicts`$\}$:

$$\text{boost}(r') = \max_{e \in \text{edges}(T, r')} \beta(e.\text{type})$$

where per-type multipliers $\beta$ are:

| edge type | $\beta$ |
|---|---|
| `supports` | 1.30 |
| `instantiates` | 1.25 |
| `cites`, `depends_on` | 1.20 |
| `co_aspect` | 1.15 |
| `co_recalled` | 1.10 |
| `contradicts` | 0.85 |

Resources in $T$ are excluded from receiving a boost (`pipeline.py:281-282`). The boost is applied multiplicatively to the RRF score of every chunk belonging to a boosted resource:

$$\text{rrf}'(c) = \text{rrf}(c) \cdot \text{boost}(c.\text{resource\_id})$$

### 1.7 Resource Aggregation

`pipeline.py:297-311`

$$\text{score}(r) = \sum_{c \in \text{top-3 chunks of } r \text{ by } \text{rrf}'} \text{rrf}'(c)$$

Resources are ranked by score descending. Top $n$ returned (default 5).

### 1.8 Explanation Generation

`concept_expand.py:115-172`

`build_why_surfaced` is called with `matched_aspects=expanded_aspects` — the list returned by `expand_query_aspects`, NOT `merged_aspects` (which includes caller-supplied filter aspects). The explanation reports overlap between `expanded_aspects` and the resource's own tags. When `project_interpretations.summary_md` exists, it is prepended: `"In this project ({stance}): {summary_md[:120]} — " + base_reason`.

### Pipeline Diagram

```
q
├─ expand_query_aspects → ε(q)
│    ├─ keywords(q): strip non-alphanum, drop stop words
│    ├─ rapidfuzz partial_ratio ≥ 70 over aspect_vocab.label (limit=5 per kw)
│    └─ 1-hop co_aspect graph expansion over matched resources' neighbors
│
├─ merged_aspects = filter_aspects ++ ε(q)    [pipeline.py:146-153]
│
├─ HyDE(q, profile_md[:500]) → h              [hyde.py:39-64]
│    └─ ε(q) NOT passed to HyDE
│
BM25(chunks_fts | aspect ∈ merged_aspects) → R_lex    [lex.py, hard JOIN filter]
ANN(embed(h)   | aspect ∈ merged_aspects) → R_ann    [vec.py, hard JOIN filter]
         ↓ RRF(k=60)
         M  (merged chunk ranking, sorted by rrf DESC)
         ↓ graph_rerank(top-5, max-pool boost, multiplicative rrf' = rrf * β)
         M' (boosted)
         ↓ group by resource_id, sum top-3 chunks' rrf'
         results[:n]
         ↓ build_why_surfaced(expanded_aspects, NOT merged_aspects)
```

---

## §2 Critique

**C1 — Hard pre-filter causes zero-recall failure** (Severity: High)

The aspect filter in both `search_lex` (`lex.py:67-72`) and `search_vec` (`vec.py:74-79`) is a hard JOIN: a chunk is excluded if none of its resource's aspects appear in $\varepsilon(q)$. When `expand_query_aspects` returns an empty list — which happens whenever `keywords(q)` produces no fuzzy matches against `aspect_vocab` (e.g., a new vocabulary term, a typo, a multi-word concept with no single-token overlap) — `merged_aspects` is non-null only if the caller passed `filter_aspects`. In the default MCP recall flow with no explicit filter, an empty $\varepsilon(q)$ means both retrievers scan the full project corpus unfiltered, which is correct. But with a caller-supplied `filter_aspects` that contains a misspelled label, the JOIN silently returns zero rows. No fallback is triggered; the user receives an empty result set with no diagnostic.

**C2 — Asymmetric HyDE grounding** (Severity: Medium)

HyDE receives `profile_md` but not $\varepsilon(q)$ (`hyde.py:50`, `pipeline.py:172`). The expanded aspects encode what the system already knows is relevant vocabulary for this query in this project; feeding them to the HyDE prompt would bias the hypothetical document toward the project's terminology and reduce the semantic gap between $\vec{h}$ and the corpus. Instead, the hypothetical is grounded only in the project profile, which may be sparse or generic. For a query about a narrow concept that maps cleanly to an aspect (e.g., "attention mechanism variants") where $\varepsilon(q) = [\text{"multi-head attention"}, \text{"positional encoding"}]$, the HyDE output could discuss those terms explicitly and produce a closer embedding to the indexed passages. The current design wastes this signal.

**C3 — Multiplicative boost on a rank-based score is ill-conditioned** (Severity: Medium)

RRF scores lie in $\left(0, \frac{2}{61}\right] \approx (0, 0.033]$ for chunks appearing in both lists, and in $\left(0, \frac{1}{61}\right] \approx (0, 0.016]$ for single-list hits. Applying a 1.30 multiplier to a chunk with rrf=0.010 yields 0.013 — a boost of 0.003. The same multiplier applied to a chunk with rrf=0.030 yields 0.039 — a boost of 0.009. Because the boost is proportional to the original RRF score, resources that were weakly ranked (low RRF) receive a smaller absolute boost than already-strong resources. This means the graph signal amplifies existing rank rather than correcting it: a poorly-matched but graph-connected resource gets a smaller nudge than a well-matched one. The intent of graph rerank is to surface contextually relevant resources that the lexical/vector search missed; the multiplicative design works against this intent.

**C4 — Max-pool aggregation silences contradictions** (Severity: Medium)

When a resource $r'$ is connected to multiple top-5 resources via different edge types — say, `supports` from $r_1$ (boost 1.30) and `contradicts` from $r_2$ (boost 0.85) — the max-pool at `pipeline.py:273-278` selects 1.30, discarding the demotion signal entirely. The `contradicts` edge type is present in `_GRAPH_EDGE_TYPES` and `_EDGE_TYPE_BOOST` with a value of 0.85, but the max aggregation guarantees it only applies if no positive-boost edge exists to the same neighbor from any top-5 resource. In a dense graph, `contradicts` effectively never fires.

**C5 — Bag-of-labels matching ignores semantic proximity** (Severity: High)

Query expansion uses `partial_ratio` string matching over label strings. Two semantically equivalent labels — e.g., "representation learning" and "feature learning" — will not match each other unless one is a substring of the other. The `Embedder` is accepted in `expand_query_aspects`'s signature precisely to enable semantic matching (`concept_expand.py:44` notes it is "unused in the current keyword-match path"), but the embedding path was never implemented. As a result, the aspect filter introduces a vocabulary boundary: content tagged with semantically related but string-distinct labels is excluded from retrieval. This is a correctness problem, not just a ranking problem — those resources are absent from the result set entirely.

**C6 — Confidence and edge weight are stored but never read** (Severity: Medium)

`resource_aspects.confidence` (default 1.0) and `concept_edges.weight` (default 1.0) are defined in the schema (`schema.sql:309`, `schema.sql:386`). The `project_resource_aspects` table also carries `weight_signals_json`. None of these are read during retrieval. The per-type $\beta$ multipliers in `_EDGE_TYPE_BOOST` are hard-coded constants; the per-edge `weight` column would allow graduated boosts (e.g., a citation with weight=0.3 representing a passing mention vs. weight=1.0 for a primary dependency), but this signal is dormant.

**C7 — Usage logs never close the feedback loop** (Severity: High)

Every MCP tool call writes a `resource_usage_log` row and enqueues `infer_interpretation` (`CLAUDE.md` pipeline description; `jobs/log_usage.py`). The co_recalled edge type is defined in the schema and listed in `_GRAPH_EDGE_TYPES`, implying that co-recalled resources should form edges. However, there is no job that materialises `co_recalled` edges from `resource_usage_log` patterns. The `infer_interpretation` job produces `concept_edges` of semantic types (`supports`, `instantiates`, etc.) from LLM analysis, not from usage co-occurrence. Positive retrieval feedback (user retrieved $r_1$ and $r_2$ in the same session) is logged but never converted into edge weight updates or boost signal. The system cannot learn from what the user actually found useful.

**C8 — Resource score is aspect-density-blind** (Severity: Low)

The resource aggregation score `score(r) = sum(rrf'(top-3 chunks))` captures how well the chunks matched the query but not how many of the expanded aspects the resource covers. A resource tagged with 4 out of 5 expanded aspects is scored identically to one tagged with 1 out of 5, as long as their chunk-level RRF scores are equal. An aspect-density term $\alpha \cdot |r.\text{aspects} \cap \varepsilon(q)| / |\varepsilon(q)|$ would reward breadth-of-coverage without requiring a separate retriever.

**C9 — `build_why_surfaced` uses `expanded_aspects`, not `merged_aspects`** (Severity: Low)

`pipeline.py:347-352` calls `build_why_surfaced(matched_aspects=expanded_aspects)`, not `merged_aspects`. When the caller supplies `filter_aspects` (e.g., the MCP `loci_recall` tool accepting an explicit `aspects=` parameter), those labels are merged into retrieval filtering but are absent from the explanation. The overlap computation in `build_why_surfaced` misses the caller-supplied labels, producing explanations that don't reflect the actual filter used. This is a cosmetic defect but misleads the user about why a resource was selected.

---

## §3 Redesign

### R1 — Soft aspect channel alongside hard filter (**implemented in v2.1**)

The binary hard JOIN has been replaced with a soft scoring path. The hard filter remains only for caller-supplied `filter_aspects`; the default is a three-way RRF:

$$\text{rrf}_3(c) = \frac{1}{60 + \text{rank}_{\text{lex}}(c)} + \frac{1}{60 + \text{rank}_{\text{vec}}(c)} + \frac{w_a}{60 + \text{rank}_{\text{asp}}(c)}$$

where $\text{rank}_{\text{asp}}(c)$ is the rank of resource $c.r$ sorted by $|\text{aspects}(c.r) \cap \varepsilon(q)|$ descending, and $w_a = 0.5$ is a tunable weight. When $\varepsilon(q) = \emptyset$, $\text{rank}_{\text{asp}}$ is undefined and the term drops out, gracefully degrading to the current two-way RRF. This requires no new tables — `resource_aspects` and `project_resource_aspects` supply the overlap count. Zero-recall failure (C1) is eliminated: resources with no aspect match still appear via lex/vec channels.

### R2 — Feed ε(q) to HyDE (**implemented in v2.1**)

`expanded_aspects` is now passed to `hyde.hypothesize` as a second context block:

```python
if expanded_aspects:
    instructions += f"\n\nRelevant concepts: {', '.join(expanded_aspects)}"
```

This was a two-line change in `hyde.py` adding an `aspects` parameter to `hypothesize(query, project_memo, aspects)`. No schema change. Addresses C2. The risk that biasing the hypothetical toward aspect terms may degrade generalization for queries where aspect expansion was noisy can still be tested by comparing NDCG with and without the hint on a held-out query set.

### R3 — Signed sum for graph boost (**implemented in v2.1**)

The max-pool has been replaced with a signed sum that allows demotion signals to accumulate:

$$\text{boost}(r') = 1 + \sum_{e \in \text{edges}(T, r')} (\beta(e.\text{type}) - 1) \cdot e.\text{weight}$$

where $e.\text{weight}$ is the dormant `concept_edges.weight` column. For a single `supports` edge with weight=1.0: boost = 1 + 0.30 = 1.30 (same as current). For `supports` (weight=1.0) + `contradicts` (weight=1.0): boost = 1 + 0.30 + (−0.15) = 1.15. For `contradicts` only: boost = 0.85. This activates the `contradicts` edge type (C4) and uses the stored weight (C6). The signed sum can in principle go below 1.0 for resources with multiple demotion edges; clamp at `max(boost, 0.5)` to prevent full suppression without explicit evidence.

### R4 — Materialise co_recalled edges from usage logs

Add a job `refresh_corecalled_edges` (analogous to the existing `refresh_project_edges.py`) that runs nightly:

```sql
-- co-occurrence count within a time window
SELECT a.resource_id, b.resource_id, COUNT(*) AS n
FROM resource_usage_log a
JOIN resource_usage_log b
  ON a.session_hash = b.session_hash
 AND a.resource_id < b.resource_id
 AND a.project_id = b.project_id
 AND a.project_id = ?
WHERE a.used_at >= datetime('now', '-30 days')
GROUP BY a.resource_id, b.resource_id
HAVING n >= 2
```

Write results as `concept_edges` with `edge_type='co_recalled'` and `weight = min(1.0, n / 10.0)`. This closes the feedback loop (C7) without requiring any new schema beyond `concept_edges.weight` already being there.

### R5 — Use edge weights in graph rerank

With R3 in place, read `concept_edges.weight` during graph rerank instead of treating all edges as weight=1.0. The `ConceptEdgeRepository.edges_from` / `edges_to` methods already return edge objects with the `weight` field from the schema; no schema change is needed, only a change to `pipeline.py:270-279` to multiply the boost delta by the edge weight.

### R6 — Aspect embeddings for semantic expansion

Precompute an embedding vector for each `aspect_vocab` label. At query time, embed $q$ (or reuse $\vec{h}$) and retrieve the top-$k$ aspects by cosine similarity:

$$\varepsilon_{\text{sem}}(q) = \text{argmax}_{a \in \mathcal{V}_p}^{k} \cos(\vec{q},\; \vec{a})$$

Merge with string-based matches: $\varepsilon(q) \leftarrow \varepsilon_{\text{string}}(q) \cup \varepsilon_{\text{sem}}(q)$. This requires the `aspect_embeddings` table described in §4. Addresses C5. Note that semantic expansion is noisier than string matching in small vocabularies; a minimum similarity threshold of 0.5 and a vocabulary size check (skip if $|\mathcal{V}_p| < 10$) are advisable guard conditions.

PMI-weighted aspect co-occurrence can also guide expansion: aspects that frequently co-occur with matched aspects in this project's corpus are promoted candidates:

$$\text{PMI}(a_1, a_2) = \log \frac{P(a_1, a_2)}{P(a_1) P(a_2)}$$

where $P(a_1, a_2)$ is the fraction of resources carrying both labels in project $p$. Computing this requires a single aggregation query over `project_resource_aspects`. Store in the `aspect_edges` table (§4, `edge_type='co_aspect_pmi'`, `weight=PMI`).

### R7 — Aspect-density score component (**implemented in v2.1**)

A density term has been added to resource scoring:

$$\text{score}(r) = \sum_{c \in \text{top-3}} \text{rrf}'(c) + \alpha \cdot \frac{|\text{aspects}(r) \cap \varepsilon(q)|}{|\varepsilon(q)|}$$

with $\alpha = 0.2$ (tunable). $\alpha / |\varepsilon(q)|$ is at most 0.2 / 1 = 0.2 when a single aspect is matched. At $|\varepsilon(q)| = 5$, the maximum density bonus is still 0.2. The typical RRF sum for a top-3 resource is in the range $[0.01, 0.10]$; an $\alpha = 0.2$ density bonus is non-negligible but not dominant. Addresses C8. Requires no schema change; `resource_aspects` or `project_resource_aspects` supplies the overlap count.

### R8 — Fix `build_why_surfaced` to use `merged_aspects` (**implemented in v2.1**)

`merged_aspects` (not `expanded_aspects`) is now passed to `build_why_surfaced`. One-line change. Addresses C9. No schema change needed.

---

## §4 Data-model delta

Three new tables are needed to support R6 (semantic expansion and PMI).

### 4.1 `aspect_edges` — typed aspect-to-aspect relations

```sql
CREATE TABLE IF NOT EXISTS aspect_edges (
    id          TEXT PRIMARY KEY,
    src_id      TEXT NOT NULL REFERENCES aspect_vocab(id) ON DELETE CASCADE,
    dst_id      TEXT NOT NULL REFERENCES aspect_vocab(id) ON DELETE CASCADE,
    project_id  TEXT,    -- NULL = global; non-NULL = project-scoped PMI
    edge_type   TEXT NOT NULL,  -- 'co_aspect_pmi' | 'semantic_sim' | 'custom'
    weight      REAL NOT NULL DEFAULT 1.0,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (src_id, dst_id, COALESCE(project_id, ''), edge_type)
);
CREATE INDEX IF NOT EXISTS idx_aspect_edges_src     ON aspect_edges(src_id, edge_type);
CREATE INDEX IF NOT EXISTS idx_aspect_edges_project ON aspect_edges(project_id);
```

This table mirrors `concept_edges` at the vocabulary level rather than the resource level. It stores both PMI co-occurrence weights (computed from `project_resource_aspects`) and cosine similarity between label embeddings (computed from `aspect_embeddings`). Having a first-class aspect graph allows `expand_query_aspects` to traverse aspect-to-aspect edges instead of the indirect resource-mediated expansion currently used (`concept_expand.py:89-103`). The `project_id` scoping allows per-project PMI without polluting the global vocabulary.

### 4.2 `aspect_embeddings` — precomputed per-label vectors

```sql
CREATE TABLE IF NOT EXISTS aspect_embeddings (
    aspect_id   TEXT PRIMARY KEY REFERENCES aspect_vocab(id) ON DELETE CASCADE,
    embedding   BLOB NOT NULL,   -- packed float32 array, same dim as chunk_vec (384)
    model_id    TEXT NOT NULL,   -- e.g. 'BAAI/bge-small-en-v1.5'
    computed_at TEXT NOT NULL
);
```

Storing embeddings in the same format as `chunk_vec` (packed float32, 384-dim) allows reuse of the existing `Embedder` and `vec_to_blob` utilities without a separate model or normalisation pipeline. The `model_id` column gates re-computation: if the embedder changes, rows with a stale `model_id` are recomputed. This table is small even at scale — a vocabulary of 1000 labels × 384 dims × 4 bytes = 1.5 MB. Computing this during `classify_aspects` (which already runs post-ingest) adds negligible cost.

### 4.3 `aspect_provenance` — append-only assignment history

```sql
CREATE TABLE IF NOT EXISTS aspect_provenance (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    resource_id TEXT NOT NULL REFERENCES raw_nodes(id) ON DELETE CASCADE,
    aspect_id   TEXT NOT NULL REFERENCES aspect_vocab(id) ON DELETE CASCADE,
    action      TEXT NOT NULL CHECK (action IN ('added', 'removed', 'confirmed', 'rejected')),
    source      TEXT NOT NULL,  -- 'user' | 'llm' | 'usage' | 'inferred'
    confidence  REAL,
    session_hash TEXT,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ap_resource ON aspect_provenance(project_id, resource_id);
CREATE INDEX IF NOT EXISTS idx_ap_aspect   ON aspect_provenance(project_id, aspect_id);
```

The current `project_resource_aspects` table is a snapshot: when an LLM re-runs `infer_interpretation`, the row is overwritten. There is no way to know whether the user previously rejected a label that the LLM re-assigned, or whether an aspect was stable across multiple inference runs. `aspect_provenance` is append-only and records every assignment and removal event with source and confidence. This enables: (a) gold-protection logic to consult history rather than relying on the `source='user'` current-row flag, (b) training signal derivation — `action='rejected'` rows are the negative examples missing from the current positive-only feedback loop (C7), and (c) audit for debugging unexpected aspect assignments.

---

## §5 Open Problems

**Cold-start and data sparsity for PMI.** PMI co-occurrence requires at least $O(1/\epsilon^2)$ resource-aspect co-occurrences to produce estimates with error $\epsilon$. For a new project with fewer than 20 resources, most PMI estimates will be unreliable. The design should fall back to string and semantic expansion only until a minimum corpus threshold (e.g., 50 resources with at least 2 aspects each) is reached. Global PMI (across all projects) is more stable but may introduce cross-project contamination.

**Vocab governance.** `aspect_vocab` has both global (`project_id IS NULL`) and project-local rows. The `aspect_edges` table inherits this ambiguity. A label promoted from local to global should have its project-scoped `aspect_edges` rows either merged or shadowed. There is no current mechanism for vocabulary normalization (deduplication, synonym merging), and the auto-growth behavior (`auto_inferred=1`) means the vocabulary can accumulate near-duplicate labels that degrade the string-matching precision of `expand_query_aspects`.

**Negative training signal.** `resource_usage_log` records every retrieval event. This is a positive-only signal: the system knows what was retrieved but not what was retrieved and found unhelpful. R4 proposes materialising `co_recalled` edges from co-occurrence, but co-occurrence in a session is not the same as mutual relevance. A user retrieving two resources on the same topic may be comparing them because they contradict each other, not because they support each other. Without an explicit disconfirmation signal (user dismisses a result, user edits aspects to remove a label after retrieval), the feedback loop will strengthen whatever the current retrieval biases happen to surface.

**Embedding model drift.** `aspect_embeddings` and `chunk_vec` must use the same model and normalisation to be comparable. If the embedder is upgraded (e.g., from bge-small-en-v1.5 to a larger model), all stored vectors must be recomputed together. The `model_id` column in `aspect_embeddings` tracks this, but `chunk_vec` has no such column — it is a `vec0` virtual table with no metadata. A coordinated migration that recomputes all chunk vectors and aspect embeddings atomically needs an explicit migration step.

**Computational cost of three-way RRF at scale.** R1 adds a third retrieval channel (aspect ranking). For small corpora (< 10,000 resources) this is negligible. At larger scales, the aspect overlap query — counting per-resource matches against up to 5 expanded aspect labels — requires a GROUP BY over `project_resource_aspects`, which is not indexed for this access pattern. An index on `(project_id, aspect_id, resource_id)` already exists as `idx_pra_project_aspect`; the overlap count can be computed as a single aggregation pass before the RRF merge, adding at most one query per retrieval call. This is acceptable.

---

## §6 Implementation Status

The v2.1 redesign has landed for the following items:

| ref | item                                                | status        |
|-----|-----------------------------------------------------|---------------|
| R1  | Soft aspect channel (3-way RRF)                     | implemented   |
| R2  | ε(q) fed into HyDE                                  | implemented   |
| R3  | Signed-sum graph rerank with edge-weight awareness  | implemented   |
| R5  | `concept_edges.weight` consumed during rerank       | implemented (folded into R3) |
| R6  | Aspect embeddings for semantic expansion            | implemented (`aspect_embeddings` + embed_aspects job) |
| R7  | Aspect-density resource score bonus                 | implemented   |
| R8  | `build_why_surfaced` uses `merged_aspects`          | implemented   |
| R4  | Materialise `co_recalled` edges from usage logs     | **proposed**  |
| —   | Calibration / NDCG harness for expansion thresholds | **proposed**  |

The two proposed items remain open: the `co_recalled` edge materialisation job from §3 R4 and a held-out evaluation harness for tuning the aspect-channel weight, density α, and rerank clamp range.
