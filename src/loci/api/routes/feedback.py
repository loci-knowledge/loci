"""Citation-level feedback endpoint.

PLAN.md treated draft citations as a one-way contract from server to client.
The agentic pipeline reverses the missing direction: the user edits the
draft, sends back the edited markdown, and we diff [Cn] markers to learn
which citations served them.

    POST /responses/:id/feedback
        body:    { edited_markdown: "..." }
        emits:   per-citation traces (cited_kept | cited_dropped | cited_replaced)
        returns: counts + reflection_job_id (a follow-up reflect cycle)

The follow-up `reflect` job re-evaluates the layer with the new feedback
folded into context. So `cited_dropped` now actively guides what the
interpreter does next.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from loci.agent import diff_citations, emit_feedback_traces
from loci.api.dependencies import db
from loci.citations import CitationTracker

router = APIRouter(prefix="/responses", tags=["feedback"])


class FeedbackBody(BaseModel):
    edited_markdown: str


@router.post("/{response_id}/feedback")
def post_feedback(
    response_id: str,
    body: FeedbackBody,
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    rec = CitationTracker(conn).get_response(response_id)
    if rec is None:
        raise HTTPException(404, detail="response not found")

    # Reconstruct the handle→node_id map for THIS response. We stored the list
    # of cited_node_ids in order, but [Cn] handles in the draft were emitted
    # in 1..k by candidate position — and the response.cited_node_ids list is
    # the deduped order they were *cited in the output*. The mapping is in
    # `request.has_context` we DON'T store; we have to recover from the
    # original output.
    handle_to_node_id = _recover_handle_map(rec)
    if not handle_to_node_id:
        raise HTTPException(
            422, detail="response has no [Cn] citations; nothing to diff",
        )

    diffs = diff_citations(
        original_md=rec["output"],
        edited_md=body.edited_markdown,
        handle_to_node_id=handle_to_node_id,
    )
    counts = emit_feedback_traces(
        conn, rec["project_id"], response_id, diffs,
    )

    # Enqueue a follow-up reflect so the layer aligns with the feedback.
    from loci.jobs.queue import enqueue
    job_id = enqueue(
        conn, kind="reflect", project_id=rec["project_id"],
        payload={"response_id": response_id, "trigger": "feedback"},
    )

    return {
        "diffs": [d.__dict__ for d in diffs],
        "counts": counts,
        "reflect_job_id": job_id,
    }


def _recover_handle_map(rec: dict) -> dict[str, str]:
    """Re-derive {handle → node_id} from a response record.

    The original draft handed out handles C1..Ck for *candidates in
    retrieval order*. We don't currently persist the full candidate list;
    we only have `cited_node_ids` (which is `[Cn]` parsed in output order).
    For the diff to work, we need handles to map to node ids consistently
    with what the LLM emitted — which means the *output's* [Cn] positions
    are authoritative.

    The map is therefore: walk the output, collect [Cn] in first-appearance
    order, pair them with cited_node_ids by appearance order.
    """
    import re
    cite_re = re.compile(r"\[C(\d+)\]", re.IGNORECASE)
    seen: list[str] = []
    for m in cite_re.finditer(rec["output"]):
        h = f"C{int(m.group(1))}"
        if h not in seen:
            seen.append(h)
    cited = rec.get("cited_node_ids") or []
    return dict(zip(seen, cited, strict=False))
