# The interpreter agent

loci's interpretation layer is built and maintained by an **agent that runs
silently after every draft**, not by a proposal queue. There is no "Accept /
Reject / Edit" review surface for new interpretations. The agent observes
what you cite, what you drop, and what you search for, then writes its
conclusions — *loci of thought*, never summaries — directly into the live
DAG at conservative confidence. Over time those loci are reinforced or
softened by your continued usage, without any explicit gesture from you.

A locus has three required slots — `relation_md`, `overlap_md`,
`source_anchor_md` — and either a `relevance` angle (closed vocabulary) or
a `philosophy`/`tension`/`decision` framing. The agent never paraphrases a
source; it identifies which part of which source matters for *this project*
and why.

## When the agent fires

| trigger       | what fired it                                           |
|---------------|---------------------------------------------------------|
| `draft`       | A `loci draft` (or the REST equivalent) just completed. |
| `feedback`    | You submitted citation-level feedback for a previous draft. |
| `kickoff`     | Project kickoff (relationship observations land directly as live nodes). |
| `manual`      | You called `loci reflect <project>`.                    |
| `retrieve`    | A `loci_retrieve` MCP tool call completed (lightweight — no self-critique). Throttled to at most one per 5 minutes per project. |
| `relevance`   | A workspace was linked to the project, the project profile changed, or incremental workspace members were added. Runs a focused single-pass synthesis without a self-critique stage. |
| `update_angle` | Triggered by citation feedback on a `relevance` node when the angle appears wrong, or by a direct user edit to such a node. Retargets the node's `angle` and `rationale_md` without recreating it. |

The first four are auto-enqueued. The agent runs in the worker thread, so
your draft response is non-blocking — you read the draft while the agent
thinks about what to add to your graph.

## What the agent decides

For every reflection cycle, the agent emits a list of **Actions**. Each
action is one of five kinds:

| kind           | effect                                                            |
|----------------|-------------------------------------------------------------------|
| `create`       | Write a new locus of thought (`tension`, `decision`, `philosophy`, or `relevance`) at confidence 0.40, `origin=agent_synthesis`. Every `create` populates the three slots: `relation_md`, `overlap_md`, `source_anchor_md`. Optionally links the new locus into the DAG. |
| `reinforce`    | Bump an existing locus's confidence by +0.05.                     |
| `soften`       | Drop an existing locus's confidence by −0.05.                     |
| `link`         | Add a directed edge between two existing nodes (or between a newly-created locus and an existing one). Valid types: `cites` (interp → raw) and `derives_from` (interp → interp). The edge repository rejects direction violations and `derives_from` cycles. |
| `update_angle` | Retarget an existing `relevance` locus's `angle` field without recreating the node. This is the refinement path for relevance routing.|

## Two-stage LLM pipeline

1. **Synthesise** (`Settings.interpretation_model`) — given the user's task,
   the candidates retrieval surfaced, the citations the draft chose, and
   the project's pinned interps as **voice anchor**, propose a list of
   Actions. The agent has handles `[N1]`..`[Nk]` for existing candidates and
   the literal `NEW` for self-references in `links`.

2. **Self-critique** (same model) — re-examine each proposed Action. Reject
   any that are too generic, that duplicate the user's pinned interps, or
   that hallucinate handles. Output `keep[]` and `drop[]` indices plus a
   one-line reason.

Only surviving Actions are applied. Everything is logged to
`agent_reflections` so you can audit the agent's reasoning.

The `relevance` job skips the self-critique stage — it is a focused
single-pass synthesis scoped to one workspace↔project pair, where the
angle vocabulary already constrains output quality.

The synthesis prompt chooses subkind from the four available types based on
what is actually observed in the candidate set:

- `tension` — open question or conflict worth pursuing
- `decision` — concrete choice with named trade-offs
- `philosophy` — grounding axiom invoked to settle disputes
- `relevance` — typed bridge between workspace(s) and project intent

The agent does not default to `relevance`; it selects `relevance` only when
the evidence spans multiple workspaces or when the relationship between a
source cluster and the project's intent is the primary thing worth naming.

## Workspace context

When a project is linked to one or more information workspaces, `_build_context()`
injects a **WORKSPACE CONTEXT** block into the synthesis prompt. This block
lists each linked workspace's name, kind, description, and up to six sample
raw titles. It gives the agent the vocabulary it needs to name bridges across
workspace boundaries.

### The `relevance` subkind

A `relevance` node expresses a typed relationship between one or more
information workspaces and the project's intent. It is always multi-source
(cites ≥2 raw nodes, ideally from different workspaces). Unlike a `decision`
(which records an inflection point) or a `tension` (which names an internal
conflict), a `relevance` node names *why a cluster of external sources matters
to this project* — what angle the connection takes.

Two required fields distinguish relevance nodes from generic interps:

- **`angle`** — one of eight named values:
  - `applicable_pattern` — a method or pattern from the workspace that applies directly
  - `experimental_setup` — a setup, apparatus, or protocol transferable to this project
  - `borrowed_concept` — a theoretical construct imported from another domain
  - `counterexample` — a case that challenges or stress-tests the project's assumptions
  - `prior_attempt` — an earlier effort at the same problem, succeeded or failed
  - `vocabulary_source` — a workspace that provides the naming conventions the project uses
  - `methodological_neighbor` — adjacent methodology that informs without being adopted wholesale
  - `contrast_baseline` — a reference point the project explicitly positions against

- **`rationale_md`** — 1–3 sentences: the "because" clause that distinguishes
  this relevance from a generic mention. The agent must supply this; nodes
  lacking it are rejected at self-critique.

### When to prefer `relevance` vs. other subkinds

Use `relevance` when the thing worth naming is the relationship between a
source cluster and the project, not something internal to the project itself.
Use `tension` for open questions and internal conflicts, `decision` for
inflection points, `philosophy` for grounding axioms. If the agent is unsure,
prefer the more specific internal subkind and let workspace context appear in
the `rationale_md` of ordinary nodes rather than forcing a `relevance` frame.

### The `update_angle` action

When citation feedback on a `relevance` node signals the angle was wrong
(e.g., the user drops a citation that was the whole basis of
`applicable_pattern`), the next reflect cycle may emit `update_angle` instead
of softening or recreating. `update_angle` carries a new `angle` value and a
new `rationale_md`, and updates the node in place. This preserves the node's
identity, edge connections, and confidence history while correcting the
interpretation.

## Citation feedback — the alignment signal

When you edit a draft and submit it back via:

```bash
loci feedback <response_id> /path/to/your-edit.md
# or POST /responses/:id/feedback {edited_markdown: "..."}
```

loci diffs the `[Cn]` markers between the original output and your edit.
Each cited node lands one of three trace kinds:

- `cited_kept` — the citation is still there, surrounding context preserved.
- `cited_replaced` — the citation is still there, but the surrounding
  sentence was rewritten substantially. Soft signal that the underlying
  node *was* on-topic but its phrasing missed.
- `cited_dropped` — the citation was removed. Strong signal that the node
  did not serve you.

These traces feed directly into the next reflection cycle's input, so
"explicitly dropped" becomes "the agent should soften that node and
consider why it was retrieved in the first place." For `relevance` nodes,
`cited_dropped` on the primary source citation additionally triggers
consideration of an `update_angle` action in the next cycle.

## Inspecting the agent

Every reflection cycle writes one row to `agent_reflections`:

```sql
SELECT
  trigger,
  instruction,
  deliberation_md,        -- the agent's free-form reasoning
  actions_json            -- the structured list of Actions actually applied
FROM agent_reflections
ORDER BY ts DESC
LIMIT 10;
```

`deliberation_md` is the agent in its own voice — what pattern it noticed,
what it decided to do, and the critique stage's keep/drop summary at the
end. Reading these regularly is the best way to see whether the agent is
synthesising things that match how you think, or drifting toward generic.

## Safety properties

- **Confidence floor** — agent-written nodes start at 0.40, well below
  pinned (typically 1.0) and explicit user creates (default 0.7). They
  show up in retrieval but rank below your own work.
- **Self-critique** — every proposed Action passes through a separate LLM
  call that explicitly looks for genericness, duplication, and bad handles.
- **Voice anchor** — the synthesis prompt ALWAYS includes your pinned
  interpretations verbatim. The agent is told to match your voice or stay
  silent.
- **Direct, not destructive** — the agent never deletes; only `dismiss`
  (an explicit user gesture) does that. Soft signals lower confidence; the
  forgetting pass at absorb time only flags candidates, never auto-prunes.
- **Audit trail** — every cycle is in `agent_reflections`. Every signal is
  in `traces`. Nothing happens that you can't reconstruct.
- **Relevance discipline** — `relevance` nodes require both `angle` and
  `rationale_md` to be accepted. The named angle forces the agent to commit
  to a specific claim about *why* a source cluster matters, not just *that*
  it is related. This acts as the alignment signal for workspace-level
  relevance, operating at the cluster level rather than the individual node
  level. Nodes missing either field are rejected at self-critique.

## Tuning

The constants in `loci/agent/interpreter.py` are the dials that matter:

```python
AGENT_BASE_CONF        = 0.4    # confidence floor for agent-written nodes
REINFORCE_DELTA        = 0.05
SOFTEN_DELTA           = -0.05
MAX_ACTIONS_PER_REFLECTION = 8
```

If the agent is being too aggressive, drop `MAX_ACTIONS_PER_REFLECTION` to
3 or 4 and raise `AGENT_BASE_CONF` to 0.5. If it's being too conservative
(skipping when it should synthesise), drop `AGENT_BASE_CONF` to 0.3.

The other knob is `Settings.interpretation_model`. Strong reasoning models
(Opus 4.x, GPT-5) produce noticeably better synthesis than fast/cheap ones.
If you're paying per-token, point `interpretation_model` at the strong one
and `classifier_model` / `hyde_model` at the fast ones.

## What this replaces

The previous design (PLAN's "Open questions" §MCP tool granularity)
treated proposals as the user-facing surface for graph extension. The
agentic pipeline removes that surface. What's left of the proposal queue:

- `broken_supports` proposals — when a raw file vanishes, the affected
  interpretation surfaces for review (you might want to keep, soften, or
  re-anchor it).
- `forgetting` proposals — long-unused, low-confidence interpretations
  surface as dismissal candidates.
- `alias` proposals — at-absorb-time only, when two interpretations are
  very near-duplicates by cosine similarity (>0.92).

These are **maintenance surface**, not the primary onboarding path. They
exist because some decisions genuinely require human judgment: did this
file actually move? Should this stale node die? Are these two interps
really the same?
