"""Citation-level feedback.

After loci returns a draft with `[Cn]` markers mapped to node ids, the user
edits the markdown. We diff the user's edited version against the original
output and emit per-node traces:

    cited_kept     — the [Cn] for that node still appears in the edit
    cited_dropped  — the [Cn] is gone (user removed the citation)
    cited_replaced — the surrounding sentence has changed materially even
                     though [Cn] still appears (user agreed with the source
                     but rewrote the claim — useful soft signal that the
                     interpretation needs refinement)

We also detect `requery`: if the user re-asks a similar query within a short
window after this draft, that's a strong signal the answer didn't satisfy.
That's the job of the retrieve route, not this module.

The trace kinds are wired into the `traces.kind` CHECK in migration 0003 and
into `Settings.forgetting_inactivity_days` cycle so kept/dropped affect
confidence over time. The interpreter reads the rolled-up summary in its
context block, so it can apply soft fixes in the next reflection cycle.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Literal

from loci.citations import CitationTracker

log = logging.getLogger(__name__)

# Same regex draft.py uses, kept here so we don't depend on the draft module.
_CITE_RE = re.compile(r"\[C(\d+)\]", re.IGNORECASE)

# How many chars of context around a [Cn] marker we sample to detect
# "the citation stayed but the surrounding sentence was rewritten." 200 chars
# either side is enough to span ~2 sentences in normal prose.
CONTEXT_WINDOW = 200

# Trigram-overlap threshold below which we consider the context "drastically
# rewritten" → cited_replaced. Above the threshold we treat as cited_kept.
# Empirical: 0.3 keeps the user-friendly default ("you didn't really change
# the sentence, just trimmed it") while still catching genuine rewrites where
# only the citation marker survived.
CONTEXT_SIMILARITY_THRESHOLD = 0.3


FeedbackKind = Literal["cited_kept", "cited_dropped", "cited_replaced"]


@dataclass
class CitationDiff:
    handle: str           # 'C1', 'C2', …
    node_id: str
    kind: FeedbackKind


def diff_citations(
    original_md: str,
    edited_md: str,
    handle_to_node_id: dict[str, str],
) -> list[CitationDiff]:
    """Compare original LLM output to the user's edited version.

    Returns one CitationDiff per handle that appeared in the original. Order:
    drops first, then replacements, then keeps — useful for log readability
    but doesn't affect correctness.
    """
    orig_handles = _handle_positions(original_md)
    edit_handles = _handle_positions(edited_md)

    out: list[CitationDiff] = []
    for handle, orig_positions in orig_handles.items():
        nid = handle_to_node_id.get(handle)
        if nid is None:
            # Unknown handle — shouldn't happen since draft.py strips these,
            # but be safe and skip.
            continue
        if handle not in edit_handles:
            out.append(CitationDiff(handle=handle, node_id=nid, kind="cited_dropped"))
            continue
        # Compare context around the *first* occurrence in each. We don't try
        # to align multi-occurrence cases — that's a rabbit hole.
        kept = _context_kept(
            original_md, edited_md,
            orig_positions[0], edit_handles[handle][0],
        )
        out.append(CitationDiff(
            handle=handle, node_id=nid,
            kind="cited_kept" if kept else "cited_replaced",
        ))
    # Sort: drops first, then replaces, then keeps.
    order = {"cited_dropped": 0, "cited_replaced": 1, "cited_kept": 2}
    out.sort(key=lambda d: order[d.kind])
    return out


def emit_feedback_traces(
    conn: sqlite3.Connection,
    project_id: str,
    response_id: str,
    diffs: list[CitationDiff],
    *,
    session_id: str = "feedback",
    client: str = "feedback",
) -> dict:
    """Write the diff results as traces. Returns a count summary."""
    tracker = CitationTracker(conn)
    counts = {"cited_kept": 0, "cited_dropped": 0, "cited_replaced": 0}
    for d in diffs:
        tracker.append_trace(
            project_id, d.node_id, d.kind,
            session_id=session_id, response_id=response_id, client=client,
        )
        counts[d.kind] += 1
    return counts


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _handle_positions(md: str) -> dict[str, list[int]]:
    """Map 'C1' → [positions in the markdown where it appeared]."""
    out: dict[str, list[int]] = {}
    for m in _CITE_RE.finditer(md):
        handle = f"C{int(m.group(1))}"
        out.setdefault(handle, []).append(m.start())
    return out


def _context_kept(orig_md: str, edit_md: str, orig_pos: int, edit_pos: int) -> bool:
    """Did the surrounding context stay roughly the same?

    Cheap shingle-overlap test: take ±CONTEXT_WINDOW chars around each
    position, normalise whitespace, count shared trigram tokens.

    Short-context fallback: if either side has <3 trigrams (i.e. fewer than
    ~5 words around the citation), we can't make a meaningful trigram
    judgment. We compare token-set overlap directly in that regime; if the
    user kept most of the surrounding words, we treat as `kept`.
    """
    a = _normalise(orig_md[max(0, orig_pos - CONTEXT_WINDOW): orig_pos + CONTEXT_WINDOW])
    b = _normalise(edit_md[max(0, edit_pos - CONTEXT_WINDOW): edit_pos + CONTEXT_WINDOW])
    sa = _shingles(a)
    sb = _shingles(b)
    if len(sa) < 3 or len(sb) < 3:
        # Fall back to token-set overlap when context is too short for trigrams.
        ta = set(a.split())
        tb = set(b.split())
        if not ta or not tb:
            return True
        token_overlap = len(ta & tb) / max(len(ta | tb), 1)
        return token_overlap >= CONTEXT_SIMILARITY_THRESHOLD
    if not sa or not sb:
        return True
    overlap = len(sa & sb) / max(len(sa | sb), 1)
    return overlap >= CONTEXT_SIMILARITY_THRESHOLD


def _normalise(s: str) -> str:
    # Strip the [Cn] markers themselves so we compare the *surrounding* text.
    return _CITE_RE.sub("", s).lower()


def _shingles(s: str, k: int = 3) -> set[str]:
    """Word-trigrams. Cheap, surprisingly robust for paragraph-scale similarity."""
    words = s.split()
    if len(words) < k:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}
