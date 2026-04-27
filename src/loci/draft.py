"""Drafting with the loci-of-thought citation contract.

The contract: a draft cites RAWS, not loci. Loci of thought are the
*routing* layer — they explain WHY each raw is the right anchor for the
project, but they are not quotable content. The user gets:

    output_md      — the answer, with [C1]…[Cn] markers pointing at raws
    citations[]    — one entry per cited raw (with chunk + entailment verdict)
    trace_table[]  — per cited raw, the chain of loci that routed retrieval to it
    routing_loci[] — the deduped set of loci used as routers (UI side panel)
    verdicts[]     — per-citation entailment judgement (supported / partial / unsupported)

Pipeline:

    retrieve (raws + winning chunks + trace_table) →
    group candidates by primary routing locus, render numbered block;
        each candidate's snippet is its CHUNK text (not a head truncation)
        so the LLM (and the verifier) see the actual span that supports
        retrieval; loci context is rendered as ROUTING-CONTEXT (not citable) →
    LLM call with raw-only citation contract →
    parse [Cn] markers, drop unknown handles →
    entailment verifier checks each (sentence, [Cn]) pair against the chunk →
    persist Response + Traces (cited + retrieved + routed_via + verdicts) →
    return DraftResult.

Why chunks: citing a 50-page PDF as the source of a sentence gives the user
no way to verify the claim. Chunk-level grounding is the foundation of
KG2RAG-style anti-hallucination — the verifier needs an actual span to
compare the claim against.

Why grouping by routing locus: KG2RAG's MST organisation packs co-routed
chunks together so the LLM reads them as a coherent story rather than as
isolated fragments. Loci's natural unit of "co-routed" is "share a locus
of thought," so candidates fan out under their primary routing locus.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Literal

from loci.citations import CitationTracker, ResponseRecord
from loci.config import get_settings
from loci.graph import NodeRepository
from loci.graph.models import NodeKind, Subkind
from loci.llm import LLMNotConfiguredError, build_agent
from loci.retrieve import RetrievalRequest, Retriever, RoutingInterp
from loci.verify import CitationVerdict, verify

log = logging.getLogger(__name__)

Style = Literal["prose", "outline", "code-comments", "bibtex"]
CiteDensity = Literal["low", "normal", "high"]

# Maximum number of candidate raws we surface to the LLM per draft call.
MAX_CANDIDATES = 40

# Snippet budgets (chars). Chunks are bounded by the chunker so we display
# them in full where possible; the cap here is just a hard ceiling for very
# rare oversized chunks. Loci context is short.
CHUNK_SNIPPET_BUDGET = 1800
LOCUS_CONTEXT_BUDGET = 320

# Citation marker regex: [C1], [c12], [C03], etc.
_CITE_RE = re.compile(r"\[C(\d+)\]", re.IGNORECASE)


@dataclass
class DraftRequest:
    project_id: str
    session_id: str
    instruction: str
    context_md: str | None = None
    anchors: list[str] = field(default_factory=list)
    style: Style = "prose"
    cite_density: CiteDensity = "normal"
    hyde: bool = False
    k: int = 12
    client: str = "unknown"
    # Run the post-draft entailment verifier? Default on. Disable for tests
    # or fast paths where the extra LLM call isn't worth the latency.
    verify: bool = True
    # Run the post-draft Self-Refine loop. Set False to skip extra LLM calls.
    refine: bool = True
    refine_max_iter: int = 2


@dataclass
class DraftCitation:
    """One cited raw + its routing trace + chunk + entailment verdict."""
    node_id: str
    kind: NodeKind                 # always 'raw' in the new model
    subkind: Subkind
    title: str
    why_cited: str                 # "matched the query directly" / "routed via 2 loci"
    # Loci that routed retrieval to this raw (interp ids in walk order).
    routed_by: list[str] = field(default_factory=list)
    # The winning chunk's id + section heading. None when the raw was reached
    # only via routing (no direct chunk hit) or for legacy non-chunked raws.
    chunk_id: str | None = None
    chunk_section: str | None = None
    # Best verdict across all (sentence, this-handle) pairs. "supported" if
    # the chunk entails any one of the sentences citing it; "partial" /
    # "unsupported" otherwise. "unknown" when verifier couldn't run.
    verdict: str = "unknown"
    verdict_reason: str = ""


@dataclass
class DraftRoutingLocus:
    """Locus surfaced to the user as routing context (not cited content)."""
    node_id: str
    subkind: Subkind
    title: str
    relation_md: str
    overlap_md: str
    source_anchor_md: str
    angle: str | None
    score: float
    # Verbose-mode fields propagated from the retrieval layer.
    channel_ranks: dict[str, int] = field(default_factory=dict)
    channel_scores: dict[str, float] = field(default_factory=dict)
    anchor_source: str | None = None


@dataclass
class DraftResult:
    output_md: str
    citations: list[DraftCitation]
    routing_loci: list[DraftRoutingLocus]
    # The full per-raw provenance the retrieval pipeline produced. The
    # citations list above is the subset the LLM actually used; trace_table
    # is the full picture for the UI.
    trace_table: list[dict]
    response_id: str
    candidate_count: int
    retrieved_node_ids: list[str]
    # Per-(sentence, handle) entailment verdicts from the verifier. Empty
    # when verification was disabled or skipped.
    verdicts: list[CitationVerdict] = field(default_factory=list)
    # Pre-rendered markdown narrative of which locus routed which raw. The
    # MCP layer ships this by default so callers see the trace shape without
    # cross-walking trace_table + routing_loci by id.
    trace_narrative: str = ""
    # Job id for the reflect job enqueued at the end of the draft pipeline,
    # or None if enqueue failed. Surfaced in `pending_effects` at the MCP
    # layer so the user knows the graph will mutate after this call.
    reflect_job_id: str | None = None
    # Number of Self-Refine iterations that ran (0 = skipped or already good).
    refine_iters: int = 0
    # Verbose-mode payload — loci that scored but routed nothing.
    pruned_loci: list[dict] = field(default_factory=list)


def draft(conn: sqlite3.Connection, req: DraftRequest) -> DraftResult:
    """Run the full draft pipeline. One LLM call for generation + one for verification."""
    # 0. Build project memo (cached; injected into generation prompt).
    from loci.agent.memo import build_project_memo, invalidate_memo_cache
    project_memo = build_project_memo(conn, req.project_id)

    # 1. Retrieve. The new pipeline returns raws + trace_table + routing_interps,
    # each raw carrying its winning chunk_id + chunk_text where available.
    retriever = Retriever(conn)
    query = (req.context_md.strip() + "\n\n" if req.context_md else "") + req.instruction
    retrieval = retriever.retrieve(RetrievalRequest(
        project_id=req.project_id, query=query, k=min(req.k, MAX_CANDIDATES),
        anchors=req.anchors, hyde=req.hyde,
    ))
    candidates = retrieval.nodes[:MAX_CANDIDATES]
    if not candidates:
        log.warning("draft: retrieval returned 0 candidates for project=%s", req.project_id)

    # Build a quick id→trace lookup so the candidate block can show the
    # routing locus context next to each raw.
    trace_by_raw = {row["raw_id"]: row for row in retrieval.trace_table}
    routing_by_id = {ri.node_id: ri for ri in retrieval.routing_interps}

    # 2. Build the candidate block + handle map. Candidates are grouped by
    # primary routing locus so the LLM reads co-routed chunks together
    # (KG2RAG's MST + DFS organisation, adapted to loci's graph).
    candidate_block, handle_to_id, handle_to_chunk_text = _format_candidates(
        candidates, conn, trace_by_raw=trace_by_raw, routing_by_id=routing_by_id,
    )

    # 3. LLM call.
    output_md, cited_handles = _generate(
        instruction=req.instruction,
        context_md=req.context_md,
        candidate_block=candidate_block,
        style=req.style,
        cite_density=req.cite_density,
        project_memo=project_memo,
    )

    # 4. Anti-fabrication: keep only handles that map to real candidates.
    cited_ids: list[str] = []
    seen: set[str] = set()
    for h in cited_handles:
        nid = handle_to_id.get(h.upper())
        if nid and nid not in seen:
            cited_ids.append(nid)
            seen.add(nid)

    # 5. Run the entailment verifier on (sentence, [Cn]) pairs.
    verdicts: list[CitationVerdict] = []
    verdict_by_handle: dict[str, CitationVerdict] = {}
    if req.verify and cited_handles:
        chunks_for_verifier = {
            h: handle_to_chunk_text[h]
            for h in cited_handles
            if h in handle_to_chunk_text and handle_to_chunk_text[h]
        }
        if chunks_for_verifier:
            vres = verify(output_md, chunks_for_verifier)
            verdicts = vres.verdicts
            # Best verdict per handle (supported beats partial beats unsupported beats unknown).
            rank = {"supported": 3, "partial": 2, "unsupported": 1, "unknown": 0}
            for v in verdicts:
                cur = verdict_by_handle.get(v.handle)
                if cur is None or rank[v.verdict] > rank[cur.verdict]:
                    verdict_by_handle[v.handle] = v

    # 5b. Rubric-aligned refinement loop (optional, controlled by req.refine).
    _refine_iters: list = []
    if req.refine and verdicts:
        try:
            from loci.agent.refine import refine_draft as _refine
            output_md, _refine_iters = _refine(
                conn=conn,
                project_id=req.project_id,
                response_id=None,
                instruction=req.instruction,
                candidate_block=candidate_block,
                output_md=output_md,
                verdicts=verdicts,
                handle_to_chunk_text=handle_to_chunk_text,
                max_iter=req.refine_max_iter,
            )
            # Re-parse cited handles from the refined output.
            cited_handles = [m.upper() for m in _CITE_RE.findall(output_md)]
            cited_ids = []
            seen: set[str] = set()
            for h in cited_handles:
                nid = handle_to_id.get(h)
                if nid and nid not in seen:
                    cited_ids.append(nid)
                    seen.add(nid)
        except Exception as exc:  # noqa: BLE001
            log.warning("draft: refinement loop failed: %s", exc)

    # 6. Build the citations[] block (only raws), now annotated with chunks
    # and best-verdict.
    citations = _materialise_citations(
        conn, candidates, cited_ids, trace_by_raw,
        handle_to_id=handle_to_id, verdict_by_handle=verdict_by_handle,
    )

    # 7. Compose routing_loci side panel (deduped, scored).
    routing_loci = [_to_routing_locus(ri) for ri in retrieval.routing_interps]

    # 8. Persist Response (with trace_table) + per-node traces.
    record = ResponseRecord(
        project_id=req.project_id, session_id=req.session_id,
        request={
            "instruction": req.instruction, "style": req.style,
            "cite_density": req.cite_density, "k": req.k,
            "hyde": req.hyde, "anchors": req.anchors,
            "has_context": req.context_md is not None,
            "verified": req.verify,
            "refine": req.refine,
            "refine_iters": len(_refine_iters),
        },
        output=output_md,
        cited_node_ids=cited_ids,
        trace_table=retrieval.trace_table,
        client=req.client,
    )
    rid = CitationTracker(conn).write_response(
        record, retrieved_node_ids=[c.node_id for c in candidates],
    )

    try:
        from loci.jobs.preference_pairs import enqueue_preference_pairs
        enqueue_preference_pairs(conn, req.project_id, rid)
    except Exception as exc:  # noqa: BLE001
        log.warning("draft: preference pair collection failed: %s", exc)

    # 9. Per-locus 'routed_via' traces — record which loci served each cited raw.
    _persist_route_traces(conn, req.project_id, req.session_id, rid,
                           cited_ids, trace_by_raw)

    # 10. Enqueue a reflection job — the interpreter agent gets to learn from
    # which loci routed which raws into a successful draft.
    reflect_job_id: str | None = None
    try:
        from loci.jobs.queue import enqueue
        reflect_job_id = enqueue(
            conn, kind="reflect", project_id=req.project_id,
            payload={"response_id": rid, "trigger": "draft"},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("draft: failed to enqueue reflect job: %s", exc)

    pruned_loci_payload = [
        {
            "id": pl.node_id, "subkind": pl.subkind, "title": pl.title,
            "score": pl.score, "reason": pl.reason,
            "channel_ranks": pl.channel_ranks,
        }
        for pl in retrieval.pruned_loci
    ]

    # Draft wrote new revisions — stale memo on the next reflect call is a bug.
    invalidate_memo_cache(req.project_id)

    return DraftResult(
        output_md=output_md,
        citations=citations,
        routing_loci=routing_loci,
        trace_table=retrieval.trace_table,
        response_id=rid,
        candidate_count=len(candidates),
        retrieved_node_ids=[c.node_id for c in candidates],
        verdicts=verdicts,
        trace_narrative=retrieval.trace_narrative,
        reflect_job_id=reflect_job_id,
        refine_iters=len(_refine_iters),
        pruned_loci=pruned_loci_payload,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _format_candidates(
    candidates,
    conn: sqlite3.Connection,
    *,
    trace_by_raw: dict[str, dict],
    routing_by_id: dict[str, RoutingInterp],
) -> tuple[str, dict[str, str], dict[str, str]]:
    """Render candidates as a numbered block, grouped by primary routing locus.

    Each candidate is a RAW node. Its winning CHUNK is the citable span; the
    routing loci are rendered as a ROUTING-CONTEXT block — *not* citable
    content, but the LLM sees them so it understands WHY this raw is
    relevant to the project.

    Returns (candidate_block, handle_to_node_id, handle_to_chunk_text).
    The chunk-text dict feeds the post-draft verifier.
    """
    nodes_repo = NodeRepository(conn)
    all_ids = [c.node_id for c in candidates]
    by_id = {n.id: n for n in nodes_repo.get_many(all_ids)}

    # Compute a primary routing locus for each candidate. Pick the highest-
    # scoring locus from the candidate's trace; fall back to "direct" when
    # the raw has no routing.
    primary_locus_by_cand: dict[str, str | None] = {}
    for cand in candidates:
        trace = trace_by_raw.get(cand.node_id)
        if not trace or not trace["interp_path"]:
            primary_locus_by_cand[cand.node_id] = None
            continue
        # First locus in the trace path is the strongest router (the trace is
        # accumulated in walk order from the highest-scoring routing interp).
        first = trace["interp_path"][0]
        primary_locus_by_cand[cand.node_id] = first["id"]

    # Group: primary_locus_id -> [candidate]. Order: groups by best score,
    # candidates within group by their own score.
    groups: dict[str | None, list] = {}
    for cand in candidates:
        groups.setdefault(primary_locus_by_cand[cand.node_id], []).append(cand)

    def _group_score(locus_id: str | None) -> float:
        if locus_id is None:
            return -1e9  # Direct-hits group goes last
        ri = routing_by_id.get(locus_id)
        return ri.score if ri else 0.0

    ordered_locus_ids = sorted(groups.keys(), key=lambda lid: -_group_score(lid))

    handle_to_id: dict[str, str] = {}
    handle_to_chunk_text: dict[str, str] = {}
    blocks: list[str] = []
    handle_n = 0

    for locus_id in ordered_locus_ids:
        group_cands = groups[locus_id]
        # Group header
        if locus_id is None:
            header = (
                "GROUP: direct hits — these raws matched the query without a "
                "routing locus."
            )
        else:
            ri = routing_by_id.get(locus_id)
            if ri is None:
                header = f"GROUP: locus {locus_id[:8]}…"
            else:
                header_lines = [
                    f"GROUP: routed via locus [{ri.subkind}] {ri.title}"
                    + (f" (angle={ri.angle})" if ri.angle else ""),
                ]
                if ri.relation_md:
                    header_lines.append(
                        f"  relation: {_truncate(ri.relation_md, LOCUS_CONTEXT_BUDGET)}",
                    )
                if ri.overlap_md:
                    header_lines.append(
                        f"  overlap:  {_truncate(ri.overlap_md, LOCUS_CONTEXT_BUDGET)}",
                    )
                if ri.source_anchor_md:
                    header_lines.append(
                        f"  anchor:   {_truncate(ri.source_anchor_md, LOCUS_CONTEXT_BUDGET)}",
                    )
                header = "\n".join(header_lines)

        # Sort candidates within the group by their own retrieval score.
        group_cands.sort(key=lambda c: -c.score)

        cand_blocks: list[str] = [header]
        for cand in group_cands:
            handle_n += 1
            handle = f"C{handle_n}"
            handle_to_id[handle] = cand.node_id
            node = by_id.get(cand.node_id)
            if node is None:
                continue
            # Snippet: the winning chunk text (post-chunker) is the first
            # choice. If retrieval gave us no chunk (routing-only or legacy),
            # fall back to the existing snippet (FTS truncation) or body head.
            snippet = cand.chunk_text or cand.snippet or _snippet_fallback(node.body)
            snippet = _truncate(snippet, CHUNK_SNIPPET_BUDGET)
            handle_to_chunk_text[handle] = cand.chunk_text or snippet

            section_line = ""
            if cand.chunk_section:
                section_line = f" section=\"{cand.chunk_section}\""

            block = (
                f"  [{handle}] kind={node.kind}/{node.subkind} "
                f"title=\"{node.title}\"{section_line}\n"
                f"  why-retrieved: {cand.why}\n"
                f"  ---\n"
                + _indent(snippet, 2)
            )
            cand_blocks.append(block)
        blocks.append("\n\n".join(cand_blocks))

    return "\n\n========\n\n".join(blocks), handle_to_id, handle_to_chunk_text


def _indent(text: str, n: int) -> str:
    pad = " " * n
    return "\n".join(pad + line if line else line for line in text.splitlines())


def _truncate(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


def _snippet_fallback(body: str) -> str:
    one_line = " ".join((body or "").split())
    return one_line[:300] + ("…" if len(one_line) > 300 else "")


def _materialise_citations(
    conn: sqlite3.Connection,
    candidates,
    cited_ids: list[str],
    trace_by_raw: dict[str, dict],
    *,
    handle_to_id: dict[str, str],
    verdict_by_handle: dict[str, CitationVerdict],
) -> list[DraftCitation]:
    """Build the citations[] block — one entry per cited raw + routing loci + verdict."""
    nodes_repo = NodeRepository(conn)
    cand_by_id = {c.node_id: c for c in candidates}
    nodes = {n.id: n for n in nodes_repo.get_many(cited_ids)}
    # Reverse handle map so we can find the verdict for a given node.
    id_to_handles: dict[str, list[str]] = {}
    for h, nid in handle_to_id.items():
        id_to_handles.setdefault(nid, []).append(h)

    out: list[DraftCitation] = []
    for nid in cited_ids:
        n = nodes.get(nid)
        if n is None:
            continue
        cand = cand_by_id.get(nid)
        why = cand.why if cand else "matched candidate"
        # Routed_by: dedup interp ids from the trace path (in order).
        routed_by: list[str] = []
        seen: set[str] = set()
        trace = trace_by_raw.get(nid)
        if trace:
            for hop in trace["interp_path"]:
                for iid in (hop["id"], hop["to"] if hop["edge"] == "derives_from" else None):
                    if iid and iid not in seen:
                        seen.add(iid)
                        routed_by.append(iid)
        # Best verdict across all handles that map to this raw.
        best_verdict = "unknown"
        best_reason = ""
        rank = {"supported": 3, "partial": 2, "unsupported": 1, "unknown": 0}
        for h in id_to_handles.get(nid, []):
            v = verdict_by_handle.get(h)
            if v and rank[v.verdict] > rank[best_verdict]:
                best_verdict = v.verdict
                best_reason = v.reason
        out.append(DraftCitation(
            node_id=nid, kind=n.kind, subkind=n.subkind, title=n.title,
            why_cited=why, routed_by=routed_by,
            chunk_id=cand.chunk_id if cand else None,
            chunk_section=cand.chunk_section if cand else None,
            verdict=best_verdict, verdict_reason=best_reason,
        ))
    return out


def _to_routing_locus(ri: RoutingInterp) -> DraftRoutingLocus:
    return DraftRoutingLocus(
        node_id=ri.node_id, subkind=ri.subkind, title=ri.title,
        relation_md=ri.relation_md, overlap_md=ri.overlap_md,
        source_anchor_md=ri.source_anchor_md, angle=ri.angle,
        score=ri.score,
        channel_ranks=ri.channel_ranks, channel_scores=ri.channel_scores,
        anchor_source=ri.anchor_source,
    )


def _persist_route_traces(
    conn: sqlite3.Connection,
    project_id: str,
    session_id: str,
    response_id: str,
    cited_raw_ids: list[str],
    trace_by_raw: dict[str, dict],
) -> None:
    """Write 'routed_via' traces for each interp that served a cited raw, and
    'route_target' traces on the raws themselves. These let the absorb job
    update locus access stats based on which routing actually paid off."""
    tracker = CitationTracker(conn)
    for raw_id in cited_raw_ids:
        trace = trace_by_raw.get(raw_id)
        if not trace:
            continue
        seen: set[str] = set()
        for hop in trace["interp_path"]:
            for iid in (hop["id"], hop.get("to") if hop["edge"] == "derives_from" else None):
                if not iid or iid in seen:
                    continue
                seen.add(iid)
                try:
                    tracker.append_trace(
                        project_id, iid, "routed_via",
                        session_id=session_id, response_id=response_id,
                        client="draft",
                    )
                except Exception as exc:  # noqa: BLE001
                    log.debug("draft: routed_via trace failed: %s", exc)
        try:
            tracker.append_trace(
                project_id, raw_id, "route_target",
                session_id=session_id, response_id=response_id,
                client="draft",
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("draft: route_target trace failed: %s", exc)


# ---------------------------------------------------------------------------
# LLM call + parsing
# ---------------------------------------------------------------------------


SYSTEM_PROMPT = """\
You are loci's draft engine. You answer the user from RAW SOURCES they have
collected, with help from the user's LOCI OF THOUGHT (routing context).

A LOCUS OF THOUGHT is a pointer, not content. Candidates are organised in
GROUPS, where each group is led by the routing locus that pointed at all
the raws inside it (relation / overlap / anchor). The locus tells you WHY
those raws matter to this project; the raws are the evidence. You may use
the locus context to decide what to write — but you MUST NOT cite a locus.
Citations land on raws only.

Each raw inside a group arrives as a single CHUNK — the specific span
that retrieval matched. That chunk is the citable evidence. If the chunk
does not say something, you may not cite the raw for it.

You will be given:
1. INSTRUCTION — what to write.
2. (optional) CONTEXT_MD — text the user has already written.
3. CANDIDATES — grouped by routing locus. Each entry is one raw chunk with
   handle [C1]…[Cn]. The GROUP header is for your understanding only; the
   raws inside it are the evidence.

Rules:
- Write in markdown.
- Cite using inline markers shaped exactly like [C1], [C7], etc.
- ONLY cite candidates that appear in CANDIDATES. Never invent a handle.
  Never write [C99] if there is no C99.
- A citation [Cn] must be supported by the chunk shown for Cn. If the
  chunk doesn't say it, don't cite Cn for it. Make a different claim or
  cite a different chunk that does.
- Use ROUTING headers to understand which part of a raw matters and why,
  but quote/cite from the raw chunk itself.
- If the candidates do not support a needed claim, write the claim WITHOUT a
  citation. Do not pad. Do not fabricate.
- The output should read as a coherent piece of writing.
"""

STYLE_HINTS: dict[Style, str] = {
    "prose": "Write flowing prose. Paragraphs of 3–6 sentences.",
    "outline": (
        "Write a structured outline with markdown headings (##, ###) and "
        "bullet points. Each bullet stands alone."
    ),
    "code-comments": (
        "Write as code-block comments suitable for placing above functions. "
        "Use # for the comment leader (or // when the language obviously needs it)."
    ),
    "bibtex": (
        "Write a literature review with explicit BibTeX-style references at "
        "the end. Each [Cn] in the body corresponds to one entry."
    ),
}

DENSITY_HINTS: dict[CiteDensity, str] = {
    "low": "Cite only the load-bearing claims. Most paragraphs may have zero citations.",
    "normal": "Cite every non-trivial factual claim.",
    "high": (
        "Be very citation-dense — every sentence with a non-trivial claim "
        "should carry at least one [Cn] marker."
    ),
}


def _generate(
    *,
    instruction: str,
    context_md: str | None,
    candidate_block: str,
    style: Style,
    cite_density: CiteDensity,
    project_memo: str = "",
) -> tuple[str, list[str]]:
    """Call the LLM. Returns (output_md, list_of_handles_in_output_order)."""
    settings = get_settings()
    try:
        agent = build_agent(settings.rag_model, instructions=SYSTEM_PROMPT)
    except LLMNotConfiguredError as exc:
        log.warning("draft: %s; returning candidate-only stub", exc)
        stub = (
            "_(loci is in unconfigured mode — set the appropriate API key for "
            f"`{settings.rag_model}`, or change `LOCI_RAG_MODEL`.)_\n\n"
            f"You asked: **{instruction}**\n\n"
            f"Top candidates retrieved from your project:\n\n{candidate_block[:2000]}"
        )
        return stub, []

    sections: list[str] = []
    if project_memo:
        sections.append(f"PROJECT MEMO:\n{project_memo}")
    sections.append(f"INSTRUCTION:\n{instruction}")
    if context_md:
        sections.append(f"CONTEXT_MD (user's draft so far):\n{context_md}")
    sections.append(f"CANDIDATES:\n\n{candidate_block}")
    sections.append(
        f"STYLE: {style} — {STYLE_HINTS[style]}\n"
        f"CITE_DENSITY: {cite_density} — {DENSITY_HINTS[cite_density]}"
    )
    user_msg = "\n\n---\n\n".join(sections)

    try:
        result = agent.run_sync(user_msg)
    except Exception as exc:  # noqa: BLE001
        log.exception("draft: LLM call failed")
        return (
            f"_(draft generation failed: {exc})_\n\nCandidates:\n\n{candidate_block[:2000]}",
            [],
        )
    output = (result.output or "").strip()
    handles = [m.group(1).upper() for m in _CITE_RE.finditer(output)]
    seen: set[str] = set()
    deduped: list[str] = []
    for h in handles:
        h = f"C{int(h)}"
        if h not in seen:
            seen.add(h)
            deduped.append(h)
    return output, deduped
