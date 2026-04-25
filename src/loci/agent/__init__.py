"""Silent agentic interpretation pipeline.

The interpreter agent runs after every draft (and on a few other high-signal
events) to autonomously update the interpretation graph based on what the
user is *actually doing*. It does NOT use the proposal queue for its primary
output — actions land directly on the live graph at conservative confidence,
and accumulate user reinforcement (or get softened) over subsequent sessions.

Flow:

    user runs `loci draft` →
    draft.py writes Response + Traces, returns to user immediately →
    draft.py enqueues a `reflect` job →
    worker picks it up:
        agent.interpreter.reflect(conn, project_id, response_id, …)
            1. gather context (instruction, candidates, citations, related)
            2. SYNTHESISE — propose Actions (create/reinforce/soften/link)
            3. SELF-CRITIQUE — filter weak/duplicative/voice-mismatched ones
            4. APPLY surviving actions to live state
            5. log to agent_reflections

The user reads the draft and edits it. They can submit the edited markdown
as feedback (`loci feedback <response_id>`); we diff [Cn] markers and emit
citation-level traces that the next reflect picks up. Over time the layer
aligns with the user's voice without explicit accept/dismiss.
"""

from loci.agent.feedback import (
    CitationDiff,
    diff_citations,
    emit_feedback_traces,
)
from loci.agent.interpreter import (
    Action,
    ActionKind,
    Reflection,
    ReflectionResult,
    reflect,
)

__all__ = [
    "Action",
    "ActionKind",
    "CitationDiff",
    "Reflection",
    "ReflectionResult",
    "diff_citations",
    "emit_feedback_traces",
    "reflect",
]
