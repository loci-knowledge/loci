"""Draft endpoint — raws-only citations + locus-routed trace table.

Response shape:
    {
      output_md:        "...",
      citations:        [{ node_id, kind=raw, subkind, title, why_cited, routed_by[] }, ...],
      routing_loci:     [{ id, subkind, title, relation_md, overlap_md,
                           source_anchor_md, angle, score }, ...],
      trace_table:      [{ raw_id, raw_title, interp_path: [{id, edge, to}, ...] }, ...],
      trace_narrative:  str,             # markdown story of the routing
      pending_effects:  [...],           # graph mutations the call triggered
      pruned_loci:      [...],           # verbose-only
      response_id:      str,
    }

Pass `verbose=true` to also receive `pruned_loci` and per-channel rank/score
breakdown on each routing locus.
"""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from loci.api.dependencies import db, project_by_id
from loci.api.publishers import publish_trace_run
from loci.graph.models import Project, now_iso

router = APIRouter(prefix="/projects", tags=["draft"])


class DraftBody(BaseModel):
    instruction: str
    context_md: str | None = None
    anchors: list[str] | None = None
    style: str = "prose"
    cite_density: str = "normal"
    session_id: str = "default"
    hyde: bool = False
    k: int = 12
    verbose: bool = False


class DraftCitationOut(BaseModel):
    node_id: str
    kind: str
    subkind: str
    title: str
    why_cited: str
    routed_by: list[str]
    # Span-level grounding (0002_chunks): identifies which chunk inside the
    # raw was the actual citable evidence. None for legacy un-chunked raws
    # or raws reached only via routing.
    chunk_id: str | None = None
    chunk_section: str | None = None
    # Post-draft entailment verdict ({supported, partial, unsupported,
    # unknown}). Surfaces hallucination risk per citation.
    verdict: str = "unknown"
    verdict_reason: str = ""


class VerdictOut(BaseModel):
    handle: str
    sentence_index: int
    verdict: str
    reason: str


class RoutingLocusOut(BaseModel):
    id: str
    subkind: str
    title: str
    relation_md: str
    overlap_md: str
    source_anchor_md: str
    angle: str | None
    score: float
    # Verbose-only fields propagated from the retrieval layer.
    channel_ranks: dict[str, int] = {}
    channel_scores: dict[str, float] = {}
    anchor_source: str | None = None


class PendingEffectOut(BaseModel):
    kind: str
    job_id: str
    trigger: str
    purpose: str


class PrunedLocusOut(BaseModel):
    id: str
    subkind: str
    title: str
    score: float
    reason: str
    channel_ranks: dict[str, int] = {}


class DraftResponseBody(BaseModel):
    output_md: str
    citations: list[DraftCitationOut]
    routing_loci: list[RoutingLocusOut]
    trace_table: list[dict]
    trace_narrative: str = ""
    pending_effects: list[PendingEffectOut] = []
    pruned_loci: list[PrunedLocusOut] = []
    response_id: str
    verdicts: list[VerdictOut] = []


@router.post("/{project_id}/draft")
def post_draft(
    body: DraftBody,
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
    user_agent: str = Header("unknown"),
) -> DraftResponseBody:
    from loci.api.routes.anchors import get_active_anchors
    from loci.retrieve.effects import pending_effects_from_reflect
    from loci.usecases.draft import run_draft

    anchors = list(body.anchors) if body.anchors is not None else get_active_anchors(project.id)
    result = run_draft(
        conn, project_id=project.id, instruction=body.instruction,
        context_md=body.context_md, anchors=anchors, style=body.style,
        cite_density=body.cite_density, hyde=body.hyde, k=body.k,
        session_id=body.session_id, client=user_agent,
    )
    routing_loci_out = [
        RoutingLocusOut(
            id=rl.node_id, subkind=rl.subkind, title=rl.title,
            relation_md=rl.relation_md, overlap_md=rl.overlap_md,
            source_anchor_md=rl.source_anchor_md, angle=rl.angle,
            score=rl.score,
            channel_ranks=rl.channel_ranks if body.verbose else {},
            channel_scores=rl.channel_scores if body.verbose else {},
            anchor_source=rl.anchor_source if body.verbose else None,
        )
        for rl in result.routing_loci
    ]
    publish_trace_run(
        project.id, response_id=result.response_id, session_id=body.session_id,
        query=body.instruction, ts=now_iso(),
        routing_loci=[loc.model_dump() for loc in routing_loci_out],
        trace_table=result.trace_table, k=body.k,
    )
    pending = [
        PendingEffectOut(**e)
        for e in pending_effects_from_reflect(result.reflect_job_id, trigger="draft")
    ]
    return DraftResponseBody(
        output_md=result.output_md,
        citations=[
            DraftCitationOut(
                node_id=c.node_id, kind=c.kind, subkind=c.subkind,
                title=c.title, why_cited=c.why_cited, routed_by=c.routed_by,
                chunk_id=c.chunk_id, chunk_section=c.chunk_section,
                verdict=c.verdict, verdict_reason=c.verdict_reason,
            )
            for c in result.citations
        ],
        routing_loci=routing_loci_out,
        trace_table=result.trace_table,
        trace_narrative=result.trace_narrative,
        pending_effects=pending,
        pruned_loci=(
            [PrunedLocusOut(**pl) for pl in result.pruned_loci]
            if body.verbose else []
        ),
        response_id=result.response_id,
        verdicts=[
            VerdictOut(
                handle=v.handle, sentence_index=v.sentence_index,
                verdict=v.verdict, reason=v.reason,
            )
            for v in result.verdicts
        ],
    )


@router.post("/{project_id}/draft/stream")
async def post_draft_stream(
    body: DraftBody,
    project: Project = Depends(project_by_id),
    conn: sqlite3.Connection = Depends(db),
    user_agent: str = Header("unknown"),
) -> StreamingResponse:
    from loci.api.routes.anchors import get_active_anchors
    from loci.usecases.draft import run_draft

    anchors = list(body.anchors) if body.anchors is not None else get_active_anchors(project.id)
    result = run_draft(
        conn, project_id=project.id, instruction=body.instruction,
        context_md=body.context_md, anchors=anchors, style=body.style,
        cite_density=body.cite_density, hyde=body.hyde, k=body.k,
        session_id=body.session_id, client=user_agent,
    )
    _routing_loci_dicts_s = [
        {
            "id": rl.node_id, "subkind": rl.subkind, "title": rl.title,
            "relation_md": rl.relation_md, "overlap_md": rl.overlap_md,
            "source_anchor_md": rl.source_anchor_md, "angle": rl.angle,
            "score": rl.score,
            **(
                {
                    "channel_ranks": rl.channel_ranks,
                    "channel_scores": rl.channel_scores,
                    "anchor_source": rl.anchor_source,
                }
                if body.verbose else {}
            ),
        }
        for rl in result.routing_loci
    ]
    publish_trace_run(
        project.id,
        response_id=result.response_id,
        session_id=body.session_id,
        query=body.instruction,
        ts=now_iso(),
        routing_loci=_routing_loci_dicts_s,
        trace_table=result.trace_table,
        k=body.k,
    )
    from loci.retrieve.effects import pending_effects_from_reflect
    pending_dicts = pending_effects_from_reflect(
        result.reflect_job_id, trigger="draft",
    )

    def generate():
        words = result.output_md.split(" ")
        for i, word in enumerate(words):
            text = word + (" " if i < len(words) - 1 else "")
            yield f"data: {json.dumps({'type': 'chunk', 'text': text})}\n\n"
        done_payload = {
            "type": "done",
            "citations": [
                {"node_id": c.node_id, "kind": c.kind, "subkind": c.subkind,
                 "title": c.title, "why_cited": c.why_cited, "routed_by": c.routed_by,
                 "chunk_id": c.chunk_id, "chunk_section": c.chunk_section,
                 "verdict": c.verdict, "verdict_reason": c.verdict_reason}
                for c in result.citations
            ],
            "routing_loci": _routing_loci_dicts_s,
            "trace_table": result.trace_table,
            "trace_narrative": result.trace_narrative,
            "pending_effects": pending_dicts,
            "pruned_loci": result.pruned_loci if body.verbose else [],
            "verdicts": [
                {"handle": v.handle, "sentence_index": v.sentence_index,
                 "verdict": v.verdict, "reason": v.reason}
                for v in result.verdicts
            ],
            "response_id": result.response_id,
        }
        yield f"data: {json.dumps(done_payload)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                              headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
