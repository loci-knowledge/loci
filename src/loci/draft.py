"""Drafting with the loci-of-thought citation contract.

The new contract: a draft cites RAWS, not loci. Loci of thought are the
*routing* layer — they explain WHY each raw is the right anchor for the
project, but they are not quotable content. The user gets:

    output_md      — the answer, with [C1]…[Cn] markers pointing at raws
    citations[]    — one entry per cited raw
    trace_table[]  — per cited raw, the chain of loci that routed retrieval to it
    routing_loci[] — the deduped set of loci used as routers (UI side panel)

Pipeline:

    retrieve (returns raws + trace_table) →
    build a numbered candidate block ([C1]…[Cn] mapped to raw ULIDs;
        each candidate carries its routing-locus context as a ROUTING-CONTEXT
        block — NOT cited material, just background) →
    call the LLM with a system prompt that mandates raw-only citation →
    parse [Cn] markers, drop unknown handles →
    persist Response + Traces (cited + retrieved + routed_via) →
    return DraftResult.

Why raws as candidates: in the loci model, raws hold the answer; loci hold the
routing. Citing a locus would be citing a pointer instead of the thing it
points at. The locus context still helps the LLM — it sees "this raw was
reached via [philosophy:CLI bridges] which says <relation>" — but the
citation lands on the raw.
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

log = logging.getLogger(__name__)

Style = Literal["prose", "outline", "code-comments", "bibtex"]
CiteDensity = Literal["low", "normal", "high"]

# Maximum number of candidate raws we surface to the LLM per draft call.
MAX_CANDIDATES = 40

# Snippet budgets (chars). Raws are documents; loci context is short.
RAW_SNIPPET_BUDGET = 800
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


@dataclass
class DraftCitation:
    """One cited raw + its routing trace."""
    node_id: str
    kind: NodeKind                 # always 'raw' in the new model
    subkind: Subkind
    title: str
    why_cited: str                 # "matched the query directly" / "routed via 2 loci"
    # Loci that routed retrieval to this raw (interp ids in walk order).
    routed_by: list[str] = field(default_factory=list)


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


def draft(conn: sqlite3.Connection, req: DraftRequest) -> DraftResult:
    """Run the full draft pipeline. Synchronous — one LLM call."""
    # 1. Retrieve. The new pipeline returns raws + trace_table + routing_interps.
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

    # 2. Build candidate block + handle map.
    candidate_block, handle_to_id = _format_candidates(
        candidates, conn, trace_by_raw=trace_by_raw, routing_by_id=routing_by_id,
    )

    # 3. LLM call.
    output_md, cited_handles = _generate(
        instruction=req.instruction,
        context_md=req.context_md,
        candidate_block=candidate_block,
        style=req.style,
        cite_density=req.cite_density,
    )

    # 4. Anti-fabrication: keep only handles that map to real candidates.
    cited_ids: list[str] = []
    seen: set[str] = set()
    for h in cited_handles:
        nid = handle_to_id.get(h.upper())
        if nid and nid not in seen:
            cited_ids.append(nid)
            seen.add(nid)

    # 5. Build the citations[] block (only raws).
    citations = _materialise_citations(conn, candidates, cited_ids, trace_by_raw)

    # 6. Compose routing_loci side panel (deduped, scored).
    routing_loci = [_to_routing_locus(ri) for ri in retrieval.routing_interps]

    # 7. Persist Response (with trace_table) + per-node traces.
    record = ResponseRecord(
        project_id=req.project_id, session_id=req.session_id,
        request={
            "instruction": req.instruction, "style": req.style,
            "cite_density": req.cite_density, "k": req.k,
            "hyde": req.hyde, "anchors": req.anchors,
            "has_context": req.context_md is not None,
        },
        output=output_md,
        cited_node_ids=cited_ids,
        trace_table=retrieval.trace_table,
        client=req.client,
    )
    rid = CitationTracker(conn).write_response(
        record, retrieved_node_ids=[c.node_id for c in candidates],
    )

    # 8. Per-locus 'routed_via' traces — record which loci served each cited raw.
    _persist_route_traces(conn, req.project_id, req.session_id, rid,
                           cited_ids, trace_by_raw)

    # 9. Enqueue a reflection job — the interpreter agent gets to learn from
    # which loci routed which raws into a successful draft.
    try:
        from loci.jobs.queue import enqueue
        enqueue(
            conn, kind="reflect", project_id=req.project_id,
            payload={"response_id": rid, "trigger": "draft"},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("draft: failed to enqueue reflect job: %s", exc)

    return DraftResult(
        output_md=output_md,
        citations=citations,
        routing_loci=routing_loci,
        trace_table=retrieval.trace_table,
        response_id=rid,
        candidate_count=len(candidates),
        retrieved_node_ids=[c.node_id for c in candidates],
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
) -> tuple[str, dict[str, str]]:
    """Render candidates as a numbered block.

    Each candidate is a RAW node. Its routing context (the loci that pointed
    at it during retrieval) is rendered as a ROUTING-CONTEXT block — *not*
    citable content, but the LLM sees it so it understands WHY this raw is
    relevant to the project.
    """
    nodes_repo = NodeRepository(conn)
    all_ids = [c.node_id for c in candidates]
    by_id = {n.id: n for n in nodes_repo.get_many(all_ids)}

    handle_to_id: dict[str, str] = {}
    lines: list[str] = []
    for i, cand in enumerate(candidates, start=1):
        handle = f"C{i}"
        handle_to_id[handle] = cand.node_id
        node = by_id.get(cand.node_id)
        if node is None:
            continue
        snippet = _truncate(node.body, RAW_SNIPPET_BUDGET)
        block = (
            f"[{handle}] kind={node.kind}/{node.subkind} title=\"{node.title}\"\n"
            f"why-retrieved: {cand.why}\n"
            f"---\n{snippet}"
        )
        # Routing context — loci whose cites edges point at this raw.
        trace = trace_by_raw.get(node.id)
        if trace and trace["interp_path"]:
            interp_ids = []
            for hop in trace["interp_path"]:
                interp_ids.append(hop["id"])
                if hop["edge"] == "derives_from":
                    interp_ids.append(hop["to"])
            unique_interp_ids: list[str] = []
            seen: set[str] = set()
            for iid in interp_ids:
                if iid not in seen:
                    seen.add(iid)
                    unique_interp_ids.append(iid)
            ctx_parts: list[str] = []
            for iid in unique_interp_ids[:3]:  # cap at 3 routers per candidate
                ri = routing_by_id.get(iid)
                if ri is None:
                    continue
                line = (
                    f"  - [{ri.subkind}] {ri.title}"
                    + (f" (angle={ri.angle})" if ri.angle else "")
                )
                if ri.relation_md:
                    line += f"\n    relation: {_truncate(ri.relation_md, LOCUS_CONTEXT_BUDGET)}"
                if ri.source_anchor_md:
                    line += f"\n    anchor: {_truncate(ri.source_anchor_md, LOCUS_CONTEXT_BUDGET)}"
                ctx_parts.append(line)
            if ctx_parts:
                block += "\n\nROUTING-CONTEXT (loci that point at this raw — DO NOT CITE):\n"
                block += "\n".join(ctx_parts)
        lines.append(block + "\n")
    return "\n\n".join(lines), handle_to_id


def _truncate(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


def _materialise_citations(
    conn: sqlite3.Connection,
    candidates,
    cited_ids: list[str],
    trace_by_raw: dict[str, dict],
) -> list[DraftCitation]:
    """Build the citations[] block — one entry per cited raw + routing loci."""
    nodes_repo = NodeRepository(conn)
    cand_by_id = {c.node_id: c for c in candidates}
    nodes = {n.id: n for n in nodes_repo.get_many(cited_ids)}
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
        out.append(DraftCitation(
            node_id=nid, kind=n.kind, subkind=n.subkind, title=n.title,
            why_cited=why, routed_by=routed_by,
        ))
    return out


def _to_routing_locus(ri: RoutingInterp) -> DraftRoutingLocus:
    return DraftRoutingLocus(
        node_id=ri.node_id, subkind=ri.subkind, title=ri.title,
        relation_md=ri.relation_md, overlap_md=ri.overlap_md,
        source_anchor_md=ri.source_anchor_md, angle=ri.angle,
        score=ri.score,
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

A LOCUS OF THOUGHT is a pointer, not content. Each candidate raw arrives with
a ROUTING-CONTEXT block listing the loci that pointed at it: those tell you
WHY this raw matters to this project, and which part of it carries the weight.
You may use that context to decide what to write — but you MUST NOT cite a
locus. Citations land on raws only. The locus is the user's reasoning about
the source; the source is the evidence.

You will be given:
1. INSTRUCTION — what to write.
2. (optional) CONTEXT_MD — text the user has already written.
3. CANDIDATES — a numbered list of [C1]…[Cn] entries. Each entry is a raw
   source. Some include a ROUTING-CONTEXT block. ROUTING-CONTEXT is for your
   understanding only. NEVER cite it.

Rules:
- Write in markdown.
- Cite using inline markers shaped exactly like [C1], [C7], etc.
- ONLY cite candidates that appear in CANDIDATES. Never invent a handle.
  Never write [C99] if there is no C99.
- Use ROUTING-CONTEXT to understand which part of a raw matters and why,
  but quote/cite from the raw itself.
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

    sections: list[str] = [f"INSTRUCTION:\n{instruction}"]
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
