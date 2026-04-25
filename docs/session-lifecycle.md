# Session lifecycle

What happens between "I just started this project" and "this is now a
working knowledge graph that compounds." The bigger-picture companion to
[getting-started.md](./getting-started.md) and [agent.md](./agent.md).

The interpretation layer is **maintained by an agent that runs after every
draft**. There is no proposal queue you have to drain. Reading
[agent.md](./agent.md) first will help everything below make more sense.

## Day 0 — kickoff

You write a profile, register sources, scan, and run kickoff:

```bash
loci project create big-bet --profile profile.md
loci source add big-bet ~/papers/big-bet/
loci scan big-bet
loci kickoff big-bet --n 8
```

After kickoff, your graph contains:

- ~50 RawNodes (whatever you scanned)
- 8 **live** question interpretations at confidence 0.5 (`origin=agent_synthesis`)
- 1 project with profile + memberships
- 0 pending proposals

The "no fabricated interpretations on day 1" rule (PLAN §Cold start) is
preserved by virtue of subkind: kickoff produces *questions only* — no
patterns, philosophies, or tensions. Questions assert nothing; they invite.

The questions are immediately retrievable. Run `loci q big-bet "<topic>"`
and they'll show up in the ranked results alongside raw sources.

You don't need to "accept" them — they're already in the graph. Your job
is to start working.

## Days 1–7 — the first interpretations

This is where the graph compounds. The pattern:

```bash
loci q big-bet "<question I'm asking myself right now>"
loci draft big-bet "<a thing I want to write>"
```

What happens behind the scenes:

1. The draft comes back with `[Cn]` citations to interpretation + raw nodes.
2. **Without you doing anything**, a `reflect` job auto-enqueues. The
   interpreter agent reads your task, the candidates surfaced, and the
   citations the draft chose. It synthesises — possibly creating a new
   pattern/decision/philosophy node, possibly reinforcing existing ones.
3. By the next session, your graph has new live nodes you didn't write.

You read the draft. If it's right, you keep it. If it's wrong, you edit
it — and you submit the edit:

```bash
loci feedback <response_id> /path/to/edited-draft.md
```

This is the core alignment loop. The agent diffs your edit against its
output:

- citations you kept → nodes get reinforced
- citations you dropped → nodes get softened
- citations you kept but rewrote the sentence around → soft signal that
  the underlying interpretation needs refinement; the next reflection
  often creates a more specific node to replace it

Within a few drafts + feedback cycles, the layer starts to *sound like
you*. The agent is told (in its synthesis prompt) to match the voice of
your pinned interpretations.

### Pinning

If a node really lands — captures something you want loci to come back to
repeatedly — pin it:

```bash
curl -X POST "http://localhost:7077/nodes/<id>/pin?project_id=<pid>"
```

Pinned nodes are anchors for Personalized PageRank in retrieval AND voice
anchors for the interpreter agent. The more you pin, the more the agent
calibrates to your taste.

### Correction

If you actively disagree with a node (not just "this draft was wrong" but
"this *interpretation* was wrong"), the agentic path doesn't currently
have a special gesture for that. Two ways to handle:

- Edit the draft to remove the citation; the next reflect will soften it,
  and over enough cycles its confidence will drop below the forgetting
  threshold.
- Use `POST /nodes/<id>` to write a new interpretation that contradicts
  it; absorb's contradiction pass will detect the tension at next absorb.

## Days 8+ — drafting

Now `loci draft` is the high-leverage operation:

```bash
loci draft big-bet "Outline the implementation plan, citing prior art." \
  --style outline --cite-density high
```

Each draft both:
1. Returns markdown with inline `[Cn]` citations resolving back to nodes,
2. Triggers a reflection that adds (or sharpens) interpretations the agent
   notices you'll need *next time*.

The graph compounds in the background while you write.

## Every ~30 sessions — absorb (housekeeping only)

```bash
loci absorb big-bet
```

Absorb is now **housekeeping**, not the primary maintenance surface:

- `replay_traces` — folds remaining cited traces into access_count + confidence.
- `orphans` — flips disconnected nodes to `dirty` so they surface for review.
- `broken_supports` — flags interps whose cited raw files have moved.
- `aliases` — proposes merges for cosine > 0.92 interp pairs.
- `forgetting` — flags long-unused, low-confidence nodes for dismissal.
- `contradiction` — runs the 3-way classifier over recent raws × top-3 interps.
- `communities` — Leiden detection (optional).

The agent's continuous reflection handles most of what absorb used to do
(creating + reinforcing + softening interps). Absorb cleans up the slow,
graph-wide, batch-shaped work that's hard to do per-draft.

You can run absorb less frequently than the old ~15-session cadence —
maybe once a week, or never if you're happy with how the layer feels.

## Months later — what the graph looks like

At ~200 drafts:

- ~500 raw nodes
- ~120 live interpretation nodes (mix of agent-synthesised and explicit)
- ~80 of those are agent-synthesised at confidence 0.4–0.7
- ~30 are pinned touchstones (your highest-conviction interpretations)
- ~600 typed edges
- 5–10 communities

The retrieval quality at this point is materially better than generic RAG
on the same papers, because PPR over your pinned + frequently-reinforced
nodes pulls in things you've *decided are connected* — not just things
that match the query string.

## Failure modes

### "The agent keeps writing generic nodes."

Likely cause: not enough pinned interpretations to anchor the agent's
voice. The agent's synthesis prompt explicitly compares against pinned —
without any, it falls back to generic phrasing.

Fix: pin 3-5 of your strongest interpretations. They become voice anchors
for every subsequent reflection.

### "The agent isn't writing anything new."

Likely cause: the project's pinned set already covers the territory the
draft surfaces. The critique stage is doing its job and dropping
near-duplicates.

Check: `SELECT * FROM agent_reflections ORDER BY ts DESC LIMIT 5;` — if
deliberation says "duplicates of pinned X" repeatedly, the layer has
reached a kind of equilibrium for that topic.

### "An agent-written node is wrong."

You don't have to dismiss it explicitly. Edit the next draft that cites
it, drop the citation, and feedback. The next reflection softens it.
Three or four cycles + low access_count → forgetting candidate at the
next absorb.

Or, if it's actively misleading: `POST /nodes/<id>/dismiss` is still
there.

### "I want to see what the agent's been doing."

```bash
sqlite3 ~/.loci/loci.sqlite \
  "SELECT trigger, instruction, deliberation_md FROM agent_reflections
   WHERE project_id = '<your-project-id>'
   ORDER BY ts DESC LIMIT 10;"
```

Or use the REST API: `GET /projects/:id/reflections` (coming next iteration).

## What's intentionally NOT in the lifecycle

- **No proposal review session.** The agent decides; you correct via use.
- **No automatic absorb trigger.** You decide when to checkpoint. The
  reflect cycle handles continuous work; absorb is for periodic
  housekeeping.
- **No global "auto-prune."** The agent softens but never deletes.
  Forgetting candidates surface at absorb; only your dismiss removes.

The shape: *the agent acts; you correct by working.*
