"""Drafting with the citation contract.

PLAN.md §API §Drafting:

    POST /projects/:id/draft → output_md + citations[] + response_id

The flow:

    retrieve (k nodes from project, anchored on the user's draft if any) →
    build a numbered candidate block ([C1]…[CK] mapped to real ULIDs) →
    call Claude with a system prompt that mandates the [Cn] citation form →
    parse [Cn] markers out of the output →
    drop unknown markers (anti-fabrication) →
    look up raw `cites→raw` neighbours for each cited interpretation →
    persist Response + Traces (cited + retrieved) →
    return DraftResult.

Why the [Cn] indirection: real ULIDs are 26 chars and burn LLM attention. A
two-character handle (C1..C40) is cheap and still uniquely maps back. We do
NOT trust the model to invent valid ULIDs — every citation it emits has to
land on a candidate we gave it.

Prompt caching: the system prompt is identical across calls (within the same
style/cite_density); we mark it `cache_control: {"type": "ephemeral"}` so
Anthropic caches it for the 5-minute window. The candidate block is
per-request and uncached.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Literal

from loci.citations import CitationTracker, ResponseRecord
from loci.config import get_settings
from loci.graph import EdgeRepository, NodeRepository
from loci.graph.models import NodeKind, Subkind
from loci.llm import LLMNotConfiguredError, build_agent
from loci.retrieve import RetrievalRequest, Retriever

log = logging.getLogger(__name__)

Style = Literal["prose", "outline", "code-comments", "bibtex"]
CiteDensity = Literal["low", "normal", "high"]

# Maximum number of candidate nodes we surface to the LLM per draft call.
# Higher → more recall, more tokens; 40 is a reasonable balance.
MAX_CANDIDATES = 40

# Per-candidate snippet budget (chars). Interpretation bodies tend to be
# short; raw bodies (PDF text) get truncated hard so the prompt stays small.
INTERP_SNIPPET_BUDGET = 800
RAW_SNIPPET_BUDGET = 600

# Regex for citation markers: [C1], [c12], [C03] etc.
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
    node_id: str
    kind: NodeKind
    subkind: Subkind
    title: str
    why_cited: str            # short, derived from retrieval channels
    raw_supports: list[str]   # raw node ids this interp `cites` (empty for raw cites)


@dataclass
class DraftResult:
    output_md: str
    citations: list[DraftCitation]
    response_id: str
    # Bookkeeping so callers can inspect what we sent to the LLM.
    candidate_count: int
    retrieved_node_ids: list[str]


def draft(conn: sqlite3.Connection, req: DraftRequest) -> DraftResult:
    """Run the full draft pipeline. Synchronous — one LLM call."""
    # 1. Retrieve.
    retriever = Retriever(conn)
    # If the user provided context_md, use it as the query body so we anchor
    # on what they're already writing. Otherwise use the instruction.
    query = (req.context_md.strip() + "\n\n" if req.context_md else "") + req.instruction
    retrieval = retriever.retrieve(RetrievalRequest(
        project_id=req.project_id, query=query, k=min(req.k, MAX_CANDIDATES),
        anchors=req.anchors, hyde=req.hyde,
    ))
    candidates = retrieval.nodes[:MAX_CANDIDATES]
    if not candidates:
        log.warning("draft: retrieval returned 0 candidates for project=%s", req.project_id)

    # 2. Build candidate block + handle map.
    candidate_block, handle_to_id = _format_candidates(candidates, conn)

    # 3. LLM call (or stub if no Anthropic configured).
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

    # 5. Build the citations[] block.
    citations = _materialise_citations(conn, candidates, cited_ids)

    # 6. Persist Response + traces.
    record = ResponseRecord(
        project_id=req.project_id, session_id=req.session_id,
        request={
            "instruction": req.instruction, "style": req.style,
            "cite_density": req.cite_density, "k": req.k,
            "hyde": req.hyde, "anchors": req.anchors,
            # Don't persist context_md — it can be huge and may contain PII.
            "has_context": req.context_md is not None,
        },
        output=output_md,
        cited_node_ids=cited_ids,
        client=req.client,
    )
    rid = CitationTracker(conn).write_response(
        record, retrieved_node_ids=[c.node_id for c in candidates],
    )

    # 7. Enqueue a reflection job. The interpreter agent runs in the worker
    # thread; the user gets the draft immediately. The reflection updates
    # the live interpretation graph silently — no proposal queue.
    try:
        from loci.jobs.queue import enqueue
        enqueue(
            conn, kind="reflect", project_id=req.project_id,
            payload={"response_id": rid, "trigger": "draft"},
        )
    except Exception as exc:  # noqa: BLE001 — never let reflect-enqueue break a draft
        log.warning("draft: failed to enqueue reflect job: %s", exc)

    return DraftResult(
        output_md=output_md, citations=citations, response_id=rid,
        candidate_count=len(candidates),
        retrieved_node_ids=[c.node_id for c in candidates],
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _format_candidates(
    candidates, conn: sqlite3.Connection,
) -> tuple[str, dict[str, str]]:
    """Render candidates as a numbered block and return (block, handle→id map)."""
    nodes_repo = NodeRepository(conn)
    by_id = {n.id: n for n in nodes_repo.get_many([c.node_id for c in candidates])}
    handle_to_id: dict[str, str] = {}
    lines: list[str] = []
    for i, cand in enumerate(candidates, start=1):
        handle = f"C{i}"
        handle_to_id[handle] = cand.node_id
        node = by_id.get(cand.node_id)
        if node is None:
            continue
        budget = INTERP_SNIPPET_BUDGET if node.kind == "interpretation" else RAW_SNIPPET_BUDGET
        snippet = _truncate(node.body, budget)
        lines.append(
            f"[{handle}] kind={node.kind}/{node.subkind} title=\"{node.title}\"\n"
            f"why-retrieved: {cand.why}\n"
            f"---\n{snippet}\n"
        )
    return "\n\n".join(lines), handle_to_id


def _truncate(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


def _materialise_citations(
    conn: sqlite3.Connection, candidates, cited_ids: list[str],
) -> list[DraftCitation]:
    """Build the citations[] block PLAN.md guarantees on every draft response."""
    nodes_repo = NodeRepository(conn)
    edges_repo = EdgeRepository(conn)
    cand_by_id = {c.node_id: c for c in candidates}
    nodes = {n.id: n for n in nodes_repo.get_many(cited_ids)}
    out: list[DraftCitation] = []
    for nid in cited_ids:
        n = nodes.get(nid)
        if n is None:
            continue
        cand = cand_by_id.get(nid)
        why = (cand.why if cand else "matched candidate")
        # If this is an interpretation, list its `cites` neighbours so the
        # client can show the underlying raw sources.
        raw_supports: list[str] = []
        if n.kind == "interpretation":
            raw_supports = [e.dst for e in edges_repo.from_node(nid, types=["cites"])]
        out.append(DraftCitation(
            node_id=nid, kind=n.kind, subkind=n.subkind, title=n.title,
            why_cited=why, raw_supports=raw_supports,
        ))
    return out


# ---------------------------------------------------------------------------
# LLM call + parsing
# ---------------------------------------------------------------------------


SYSTEM_PROMPT = """\
You are loci's draft engine. Your job is to write a high-quality piece of \
text using the user's own knowledge graph as evidence.

You will be given:
1. An INSTRUCTION (what to write).
2. (optional) CONTEXT_MD: text the user has already written.
3. CANDIDATES: a numbered list of [C1]..[Cn] entries. Each entry is a node \
from the user's interpretation graph or a raw source they have read. The \
"kind" field tells you which.

Rules — follow these strictly:

- Write in markdown.
- Cite using inline markers shaped exactly like [C1], [C7], etc. — the same \
brackets and case as the candidate handles.
- ONLY cite candidates that appear in the CANDIDATES list. Never invent a \
handle. Never write [C99] if there is no C99.
- Prefer interpretation candidates over raw — the user's interpretations \
encode their *why*. Cite the raw only when the claim is direct ("the paper \
shows X").
- If the candidates do not support a needed claim, write the claim WITHOUT a \
citation. Do not pad. Do not fabricate.
- The output should read as a coherent piece of writing. Citations are \
in-line evidence, not ornamental footnotes.
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
    """Call the LLM. Returns (output_md, list_of_handles_in_output_order).

    Uses `Settings.rag_model` via pydantic-ai. The system prompt is set as
    `instructions=`; for Anthropic models pydantic-ai applies prompt caching
    automatically via `AnthropicModelSettings`. Other providers ignore the
    cache hint.
    """
    settings = get_settings()
    try:
        agent = build_agent(settings.rag_model, instructions=SYSTEM_PROMPT)
    except LLMNotConfiguredError as exc:
        # No LLM configured — surface the candidates we *would* have used so
        # callers see something actionable, and return no citations.
        log.warning("draft: %s; returning candidate-only stub", exc)
        stub = (
            "_(loci is in unconfigured mode — set the appropriate API key for "
            f"`{settings.rag_model}`, or change `LOCI_RAG_MODEL`.)_\n\n"
            f"You asked: **{instruction}**\n\n"
            f"Top candidates retrieved from your project:\n\n{candidate_block[:2000]}"
        )
        return stub, []

    # Build a single user message with all the parts. We *could* split into
    # multiple text parts for finer cache breakpoints, but the system-prompt
    # cache (handled by AnthropicModelSettings) covers the bulk; the user
    # message is per-call by definition.
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
    except Exception as exc:  # noqa: BLE001 — surface, but don't crash the request
        log.exception("draft: LLM call failed")
        return (
            f"_(draft generation failed: {exc})_\n\nCandidates:\n\n{candidate_block[:2000]}",
            [],
        )
    output = (result.output or "").strip()
    handles = [m.group(1).upper() for m in _CITE_RE.finditer(output)]
    # Deduplicate while preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for h in handles:
        h = f"C{int(h)}"
        if h not in seen:
            seen.add(h)
            deduped.append(h)
    return output, deduped
