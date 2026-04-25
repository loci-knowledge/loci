# The interpreter agent

loci's interpretation layer is built and maintained by an **agent that runs
silently after every draft**, not by a proposal queue. There is no "Accept /
Reject / Edit" review surface for new interpretations. The agent observes
what you cite, what you drop, and what you search for, then writes its
conclusions directly into the live graph at conservative confidence. Over
time those interpretations are reinforced or softened by your continued
usage, without any explicit gesture from you.

## When the agent fires

| trigger    | what fired it                                           |
|------------|---------------------------------------------------------|
| `draft`    | A `loci draft` (or the REST equivalent) just completed. |
| `feedback` | You submitted citation-level feedback for a previous draft. |
| `kickoff`  | Project kickoff (questions land directly as live nodes). |
| `manual`   | You called `loci reflect <project>`.                    |

The first three are auto-enqueued. The agent runs in the worker thread, so
your draft response is non-blocking — you read the draft while the agent
thinks about what to add to your graph.

## What the agent decides

For every reflection cycle, the agent emits a list of **Actions**. Each
action is one of four kinds:

| kind        | effect                                                            |
|-------------|-------------------------------------------------------------------|
| `create`    | Write a new interpretation node (philosophy, pattern, tension, decision, question, touchstone, experiment, metaphor) at confidence 0.40, `origin=agent_synthesis`. Optionally link it into the existing graph. |
| `reinforce` | Bump an existing node's confidence by +0.05.                      |
| `soften`    | Drop an existing node's confidence by −0.05.                      |
| `link`      | Add a typed edge between two existing nodes (or between a newly-created node and an existing one). |

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
consider why it was retrieved in the first place."

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
