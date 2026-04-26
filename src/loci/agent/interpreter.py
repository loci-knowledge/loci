"""The interpreter agent — silent post-draft reflection.

Two-stage LLM pipeline:

    1. SYNTHESISE — given the user's task + retrieved candidates + citation
       feedback + the project's pinned loci as voice anchor, the agent proposes
       Actions: create / reinforce / soften / link / update_angle.

    2. SELF-CRITIQUE — same model rejects actions that don't match voice,
       duplicate live loci, or paraphrase a candidate. Only survivors apply.

A LOCUS OF THOUGHT is a routing pointer, not a summary. Every `create` action
must produce a locus with three populated slots:
    relation_md       (how source relates to project)
    overlap_md        (the concrete intersection)
    source_anchor_md  (which part of which source carries the weight)
And edges:
    cites         interp → raw   (the anchored source)
    derives_from  interp → interp (this locus builds on that one)
There are no symmetric edges; cycles in derives_from are rejected at insert.

Failure modes: missing LLM key → skipped row; LLM error → deliberation_md
records ERROR; bad output → pydantic-ai schema reject treated as LLM error.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from loci.citations import CitationTracker
from loci.config import get_settings
from loci.graph import EdgeRepository, NodeRepository, ProjectRepository
from loci.graph.models import (
    EdgeType,
    InterpretationNode,
    InterpretationSubkind,
    RelevanceAngle,
    new_id,
)
from loci.graph.workspaces import WorkspaceRepository
from loci.llm import LLMNotConfiguredError, build_agent

log = logging.getLogger(__name__)

# Confidence floors. New agent-written interps land at AGENT_BASE_CONF; the
# same node climbs (via reinforce, citation_kept) and falls (via soften,
# citation_dropped, requery) over time.
AGENT_BASE_CONF = 0.4
REINFORCE_DELTA = 0.05
SOFTEN_DELTA = -0.05

# How many of the agent's proposed actions to even bother critiquing per
# reflection. Cap keeps cost bounded; larger graphs over time mean more
# *potential* synthesis material.
MAX_ACTIONS_PER_REFLECTION = 8

ActionKind = Literal["create", "reinforce", "soften", "link", "update_angle"]


# ---------------------------------------------------------------------------
# Structured LLM output schemas
# ---------------------------------------------------------------------------


class _Link(BaseModel):
    src_handle: str   # 'NEW' for the action's create, or [Nxx] for an existing handle
    dst_handle: str
    type: EdgeType


class Action(BaseModel):
    """One thing the agent wants to do to the graph."""
    action: ActionKind
    # When action == "create": every locus needs the three slots.
    subkind: InterpretationSubkind | None = None
    title: Annotated[str | None, Field(max_length=200)] = None
    body: str | None = None  # optional free-form context, ≤300 chars
    relation_md: str | None = None
    overlap_md: str | None = None
    source_anchor_md: str | None = None
    # For relevance subkind: typed angle.
    angle: RelevanceAngle | None = None
    # When action ∈ {reinforce, soften, link, update_angle}: handle of an
    # existing node from the candidate block ([N1]..[Nk]).
    target_handle: str | None = None
    # One-line justification (for the reflection log).
    reason: Annotated[str, Field(max_length=400)] = ""
    # Optional links to add. Each handle resolves to an existing [Nxx] or the
    # literal 'NEW' meaning the just-created node. Edge type ∈
    # {cites, derives_from} — and direction must be valid for the type.
    links: list[_Link] = Field(default_factory=list)


class Reflection(BaseModel):
    """The full structured output of the SYNTHESISE stage."""
    deliberation: Annotated[str, Field(max_length=4000)]
    actions: list[Action]


class CritiqueDecision(BaseModel):
    keep: list[int]      # indices of actions to keep
    drop: list[int]      # indices of actions to drop
    notes: str = ""      # one-line summary of WHY


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@dataclass
class ReflectionResult:
    reflection_id: str
    actions_taken: int
    actions_dropped: int
    skipped: bool = False
    skip_reason: str | None = None


def reflect(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    response_id: str | None,
    trigger: Literal["draft", "feedback", "manual", "kickoff", "retrieve"] = "draft",
    lightweight: bool = False,
) -> ReflectionResult:
    """Run one reflection cycle. Returns a summary; full audit is in the DB.

    `response_id` may be None for manual reflections; in that case we still
    pull the project's pinned set as voice anchors and the recent traces, but
    there's no specific draft we're reflecting on.
    """
    settings = get_settings()

    # Build context first (cheap; lets us bail before LLM cost when nothing
    # interesting happened).
    ctx = _build_context(conn, project_id, response_id)
    if ctx is None:
        return _log_and_return(
            conn, project_id, response_id, trigger,
            instruction="(empty)",
            deliberation="No reflectable signal — neither candidates nor citations.",
            actions=[],
            applied=0,
            skipped=True, reason="no_signal",
        )

    # Synthesise.
    try:
        synth_agent = build_agent(
            settings.interpretation_model,
            instructions=_SYNTH_INSTRUCTIONS,
            output_type=Reflection,
        )
    except LLMNotConfiguredError as exc:
        return _log_and_return(
            conn, project_id, response_id, trigger,
            instruction=ctx.instruction,
            deliberation=f"SKIPPED: {exc}",
            actions=[],
            applied=0,
            skipped=True, reason=str(exc),
        )

    try:
        synth = synth_agent.run_sync(ctx.user_prompt).output
    except Exception as exc:  # noqa: BLE001
        log.exception("reflect: synthesise failed")
        return _log_and_return(
            conn, project_id, response_id, trigger,
            instruction=ctx.instruction,
            deliberation=f"ERROR (synthesise): {exc}",
            actions=[],
            applied=0,
            skipped=True, reason="synth_error",
        )

    actions = synth.actions[:MAX_ACTIONS_PER_REFLECTION]
    if not actions:
        return _log_and_return(
            conn, project_id, response_id, trigger,
            instruction=ctx.instruction,
            deliberation=synth.deliberation,
            actions=[],
            applied=0,
        )

    if lightweight:
        # skip critique, apply all
        applied = _apply_actions(conn, project_id, response_id, ctx, actions)
        deliberation = synth.deliberation + "\n\n(lightweight: critique skipped)"
        return _log_and_return(
            conn, project_id, response_id, trigger,
            instruction=ctx.instruction,
            deliberation=deliberation,
            actions=[a.model_dump() for a in actions],
            applied=applied,
        )

    # Self-critique.
    try:
        critique_agent = build_agent(
            settings.interpretation_model,
            instructions=_CRITIQUE_INSTRUCTIONS,
            output_type=CritiqueDecision,
        )
        critique_prompt = _critique_prompt(ctx, actions)
        decision = critique_agent.run_sync(critique_prompt).output
    except Exception as exc:  # noqa: BLE001
        log.exception("reflect: critique failed; applying ALL synthesised actions")
        decision = CritiqueDecision(keep=list(range(len(actions))), drop=[], notes=f"critique_error: {exc}")

    keep_idx = set(decision.keep) - set(decision.drop)
    surviving = [a for i, a in enumerate(actions) if i in keep_idx]

    # Apply.
    applied = _apply_actions(conn, project_id, response_id, ctx, surviving)

    deliberation = (
        f"{synth.deliberation}\n\n---\nCRITIQUE: {decision.notes}\n"
        f"keep={sorted(decision.keep)} drop={sorted(decision.drop)}"
    )
    return _log_and_return(
        conn, project_id, response_id, trigger,
        instruction=ctx.instruction,
        deliberation=deliberation,
        actions=[a.model_dump() for a in surviving],
        applied=applied,
        dropped=len(actions) - applied,
    )


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------


@dataclass
class _Context:
    instruction: str
    user_prompt: str
    candidate_handle_to_id: dict[str, str]   # 'N1' → ULID
    cited_node_ids: set[str]
    pinned_node_ids: list[str]


def _build_context(
    conn: sqlite3.Connection, project_id: str, response_id: str | None,
) -> _Context | None:
    """Gather everything the synthesiser needs into one prompt."""
    pr = ProjectRepository(conn)
    ws_repo = WorkspaceRepository(conn)
    project = pr.get(project_id)
    if project is None:
        return None
    pinned_ids = pr.members(project_id, roles=["pinned"])

    instruction = "(no specific task)"
    candidates: list[dict] = []
    cited_set: set[str] = set()
    feedback_summary = ""
    if response_id is not None:
        rec = CitationTracker(conn).get_response(response_id)
        if rec is not None:
            req = rec.get("request") or {}
            instruction = req.get("instruction") or req.get("query") or instruction
            cited_set = set(rec.get("cited_node_ids") or [])
            # Recover all retrieved-or-cited nodes for this response from traces.
            rows = conn.execute(
                """
                SELECT DISTINCT node_id, kind FROM traces WHERE response_id = ?
                """,
                (response_id,),
            ).fetchall()
            retrieved_ids = [r["node_id"] for r in rows]
            candidates = _materialise_candidates(conn, retrieved_ids, cited_set)
            feedback_summary = _summarise_citation_feedback(conn, response_id)

    pinned_block = _materialise_pinned(conn, pinned_ids)
    workspace_block = _materialise_workspace_context(conn, ws_repo, project_id)

    if not candidates and not pinned_block:
        return None

    handle_to_id: dict[str, str] = {}
    cand_lines: list[str] = []
    for i, c in enumerate(candidates, start=1):
        handle = f"N{i}"
        handle_to_id[handle] = c["id"]
        cite_marker = " (cited in draft)" if c["id"] in cited_set else ""
        cand_lines.append(
            f"[{handle}] kind={c['kind']}/{c['subkind']} title={c['title']!r}{cite_marker}\n"
            f"    {c['snippet']}"
        )
    candidate_block = "\n\n".join(cand_lines) if cand_lines else "(none)"

    user_prompt = (
        f"PROJECT PROFILE:\n{project.profile_md or '(empty)'}\n\n"
        f"---\n\nUSER'S CURRENT TASK:\n{instruction}\n\n"
        f"---\n\nPINNED INTERPRETATIONS (the user's voice — match this style):\n"
        f"{pinned_block or '(none)'}\n\n"
        f"---\n\nWORKSPACE CONTEXT (information workspaces linked to this project):\n"
        f"{workspace_block or '(no workspaces linked)'}\n\n"
        f"---\n\nCANDIDATES SURFACED FOR THIS TASK:\n{candidate_block}\n\n"
        f"---\n\nCITATION FEEDBACK (if any):\n{feedback_summary or '(none yet)'}\n"
    )

    return _Context(
        instruction=instruction,
        user_prompt=user_prompt,
        candidate_handle_to_id=handle_to_id,
        cited_node_ids=cited_set,
        pinned_node_ids=pinned_ids,
    )


def _materialise_candidates(
    conn: sqlite3.Connection, node_ids: list[str], cited_set: set[str],
) -> list[dict]:
    if not node_ids:
        return []
    placeholders = ",".join("?" * len(node_ids))
    rows = conn.execute(
        f"""
        SELECT id, kind, subkind, title, body
        FROM nodes
        WHERE id IN ({placeholders})
        """,
        tuple(node_ids),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        body = (r["body"] or "").strip()
        snippet = body[:400] + ("…" if len(body) > 400 else "")
        out.append({
            "id": r["id"], "kind": r["kind"], "subkind": r["subkind"],
            "title": r["title"], "snippet": snippet.replace("\n", " "),
        })
    # Cited nodes first — they're the highest-signal subset.
    out.sort(key=lambda c: c["id"] not in cited_set)
    return out


def _materialise_pinned(conn: sqlite3.Connection, pinned_ids: list[str]) -> str:
    if not pinned_ids:
        return ""
    placeholders = ",".join("?" * len(pinned_ids))
    rows = conn.execute(
        f"""
        SELECT subkind, title, body
        FROM nodes
        WHERE id IN ({placeholders}) AND kind = 'interpretation'
        """,
        tuple(pinned_ids),
    ).fetchall()
    chunks: list[str] = []
    for r in rows:
        body = (r["body"] or "").strip()[:300]
        chunks.append(f"- [{r['subkind']}] {r['title']}\n  {body}")
    return "\n".join(chunks)


def _materialise_workspace_context(
    conn: sqlite3.Connection, ws_repo: WorkspaceRepository, project_id: str,
    *,
    recent_raws: int = 6,
) -> str:
    """Render linked workspace summaries for the synthesis prompt."""
    links = ws_repo.linked_workspaces(project_id)
    if not links:
        return ""
    chunks: list[str] = []
    for ws, link in links:
        if link.role == "excluded":
            continue
        header = f"[{ws.kind}] {ws.name} (role={link.role})"
        if ws.description_md:
            header += f"\n  {ws.description_md[:200]}"
        # Sample recent raws from this workspace.
        rows = conn.execute(
            """
            SELECT n.title, n.subkind
            FROM nodes n
            JOIN workspace_membership wm ON wm.node_id = n.id
            JOIN raw_nodes r ON r.node_id = n.id
            WHERE wm.workspace_id = ?
              AND r.source_of_truth = 1
            ORDER BY n.created_at DESC
            LIMIT ?
            """,
            (ws.id, recent_raws),
        ).fetchall()
        if rows:
            titles = [f"    - [{r['subkind']}] {r['title']}" for r in rows]
            header += "\n  Recent sources:\n" + "\n".join(titles)
        chunks.append(header)
    return "\n\n".join(chunks)


def _summarise_citation_feedback(conn: sqlite3.Connection, response_id: str) -> str:
    """Roll up citation-level traces for this response into a short prose
    summary the LLM can use as input."""
    rows = conn.execute(
        """
        SELECT t.kind AS tk, n.title AS title
        FROM traces t
        JOIN nodes n ON n.id = t.node_id
        WHERE t.response_id = ? AND t.kind IN
              ('cited_kept','cited_dropped','cited_replaced','requery')
        """,
        (response_id,),
    ).fetchall()
    if not rows:
        return ""
    buckets: dict[str, list[str]] = {}
    for r in rows:
        buckets.setdefault(r["tk"], []).append(r["title"])
    lines: list[str] = []
    for kind, titles in buckets.items():
        lines.append(f"{kind}: " + "; ".join(titles[:6]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Action application
# ---------------------------------------------------------------------------


def _apply_actions(
    conn: sqlite3.Connection,
    project_id: str,
    response_id: str | None,
    ctx: _Context,
    actions: list[Action],
) -> int:
    """Apply surviving actions to the live graph. Returns count actually applied."""
    nodes_repo = NodeRepository(conn)
    edges_repo = EdgeRepository(conn)
    pr = ProjectRepository(conn)
    tracker = CitationTracker(conn)

    # Build a per-action handle map: each create produces a new id we may
    # link to via 'NEW'. We process in two passes: creates first (so 'NEW'
    # references resolve), then reinforce/soften, then standalone links.
    new_id_for_action: dict[int, str] = {}
    applied = 0

    # Pre-encode embeddings for `create` actions in a batch.
    creates = [(i, a) for i, a in enumerate(actions) if a.action == "create" and a.title and a.body]
    if creates:
        from loci.embed.local import get_embedder
        try:
            embedder = get_embedder()
            texts = [f"{a.title}\n\n{a.body}" for _, a in creates]
            vecs = embedder.encode_batch(texts)
        except Exception as exc:  # noqa: BLE001 — never let embedding failure stop the agent
            log.warning("reflect: embedding batch failed: %s", exc)
            vecs = None
        for k, (i, a) in enumerate(creates):
            embedding = vecs[k] if vecs is not None else None
            try:
                node = InterpretationNode(
                    subkind=a.subkind or "decision",
                    title=a.title or "(untitled)",
                    body=a.body or "",
                    relation_md=(a.relation_md or "").strip(),
                    overlap_md=(a.overlap_md or "").strip(),
                    source_anchor_md=(a.source_anchor_md or "").strip(),
                    angle=a.angle,
                    rationale_md="",
                    origin="agent_synthesis",
                    origin_response_id=response_id,
                    confidence=AGENT_BASE_CONF,
                    status="live",
                )
                nodes_repo.create_interpretation(node, embedding=embedding)
                pr.add_member(project_id, node.id, role="included", added_by="agent")
                tracker.append_trace(project_id, node.id, "agent_synthesised",
                                       response_id=response_id, client="agent")
                new_id_for_action[i] = node.id
                applied += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("reflect: create failed: %s", exc)

    # Reinforce / soften.
    for a in actions:
        target_id = _resolve_handle(a.target_handle, ctx, new_id_for_action)
        if a.action == "reinforce":
            if target_id is None:
                continue
            try:
                nodes_repo.bump_confidence(target_id, REINFORCE_DELTA)
                tracker.append_trace(project_id, target_id, "agent_reinforced",
                                       response_id=response_id, client="agent")
                applied += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("reflect: reinforce failed: %s", exc)
        elif a.action == "soften":
            if target_id is None:
                continue
            try:
                nodes_repo.bump_confidence(target_id, SOFTEN_DELTA)
                tracker.append_trace(project_id, target_id, "agent_softened",
                                       response_id=response_id, client="agent")
                applied += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("reflect: soften failed: %s", exc)
        elif a.action == "update_angle":
            if target_id is None:
                continue
            try:
                nodes_repo.set_angle(target_id, a.angle, "")
                tracker.append_trace(project_id, target_id, "agent_updated_angle",
                                       response_id=response_id, client="agent")
                applied += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("reflect: update_angle failed: %s", exc)

    # Links — for both standalone `link` actions and links carried on a `create`.
    for i, a in enumerate(actions):
        for link in a.links:
            src = _resolve_handle(link.src_handle, ctx, new_id_for_action, fallback_action_idx=i)
            dst = _resolve_handle(link.dst_handle, ctx, new_id_for_action, fallback_action_idx=i)
            if src is None or dst is None or src == dst:
                continue
            try:
                edges_repo.create(src, dst, type=link.type, created_by="system")
                applied += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("reflect: link %s→%s (%s) failed: %s", src, dst, link.type, exc)

    return applied


def _resolve_handle(
    handle: str | None,
    ctx: _Context,
    new_id_for_action: dict[int, str],
    *,
    fallback_action_idx: int | None = None,
) -> str | None:
    """Resolve a handle to a node id.

    - 'N1'..'Nk' → ctx.candidate_handle_to_id
    - 'NEW' → new_id_for_action[fallback_action_idx]
    """
    if handle is None:
        return None
    handle = handle.strip().upper()
    if handle == "NEW":
        if fallback_action_idx is None:
            return None
        return new_id_for_action.get(fallback_action_idx)
    return ctx.candidate_handle_to_id.get(handle)


# ---------------------------------------------------------------------------
# Reflection log + return
# ---------------------------------------------------------------------------


def _log_and_return(
    conn: sqlite3.Connection,
    project_id: str,
    response_id: str | None,
    trigger: str,
    *,
    instruction: str,
    deliberation: str,
    actions: list,
    applied: int,
    dropped: int = 0,
    skipped: bool = False,
    reason: str | None = None,
) -> ReflectionResult:
    rid = new_id()
    conn.execute(
        """
        INSERT INTO agent_reflections(id, project_id, response_id, trigger,
                                       instruction, deliberation_md, actions_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (rid, project_id, response_id, trigger, instruction, deliberation,
         json.dumps(actions)),
    )
    return ReflectionResult(
        reflection_id=rid,
        actions_taken=applied,
        actions_dropped=dropped,
        skipped=skipped,
        skip_reason=reason,
    )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


_SYNTH_INSTRUCTIONS = """\
You are loci's interpreter agent. You maintain the user's personal LOCI-OF-THOUGHT
graph — a directed acyclic graph in which every interpretation node is a
*pointer* into source space, not a summary of any source.

THE LOCUS PRINCIPLE
A locus says: "the part of THIS source over here meets the part of THIS project
over there, in this specific way, and that is why this region of source-space
matters." Loci are how retrieval finds its way back to the right paragraph of
the right source. The body of a locus is never the answer; the cited raw is the
answer, and the locus tells you which part to read and why.

INPUT YOU RECEIVE:
- PROJECT PROFILE: the user's stated goal for this project.
- USER'S CURRENT TASK: what they just asked loci to help with.
- PINNED INTERPRETATIONS: the user's voice. Match style and specificity.
- WORKSPACE CONTEXT: workspaces linked to this project, kinds, and recent
  source titles — use this to ground source_anchor_md.
- CANDIDATES: nodes that retrieval surfaced for this task with [Nxx] handles.
  Some are marked '(cited in draft)' — those served the user's output.
- CITATION FEEDBACK: cited_kept / cited_dropped / cited_replaced / requery
  signals from past drafts. cited_dropped = the locus failed to route the
  user; consider softening or rewriting.

YOUR JOB
Decide which new loci would route the user better next time, and which
existing loci to reinforce or soften.

EVERY `create` ACTION MUST POPULATE THE THREE SLOTS:
  relation_md       — 1–3 sentences. How does this source RELATE to this
                      project? Concrete bridge, not a paraphrase.
  overlap_md        — 1–2 sentences. WHERE do they intersect? Be specific.
  source_anchor_md  — Which PART of which source carries the weight? Quote a
                      phrase, name a section, point at a function/file/line,
                      identify a definition. NOT a whole-document summary.
                      If multiple sources, anchor each one separately.

Subkinds (pick what fits — do NOT default to relevance):
  - `relevance` — typed bridge across distinct sources. REQUIRES angle from
    {applicable_pattern, experimental_setup, borrowed_concept, counterexample,
     prior_attempt, vocabulary_source, methodological_neighbor,
     contrast_baseline}. cite ≥2 raws when possible.
  - `philosophy` — first-principle belief the sources reveal the project
    should hold. relation_md says how it shows up; source_anchor_md points at
    where it is stated/embodied.
  - `tension` — an unresolved conflict between sources and project, or
    between two values in the project itself. source_anchor_md points at
    where each side is visible.
  - `decision` — a concrete choice with explicit trade-offs. source_anchor_md
    cites the evidence on each side.

ACTIONS
  create        — a new locus. Required: subkind, title, three slots.
                  Strongly encouraged: a `cites` link to ≥1 raw candidate
                  (src='NEW', dst=[Nxx], type='cites'). For relevance, ≥2.
                  Optional: derives_from links to existing loci this locus
                  builds on (src='NEW', dst=[Nxx], type='derives_from').
  reinforce     — existing locus deserves more weight (target_handle).
  soften        — existing locus mis-routed the user (target_handle).
  link          — add a single edge between two existing handles.
  update_angle  — refine angle on an existing relevance locus.

EDGE TYPES (only these two; both directed; no symmetric edges):
  cites          interp → raw    (the anchored source)
  derives_from   interp → interp (this locus builds on that locus)
The graph must remain acyclic — derives_from edges that close a cycle are
rejected. Raws are leaves; never write a link with src=a raw handle.

RULES:
- Be conservative. One high-signal locus beats five generic ones.
- Match the user's voice. Mirror the specificity of their pinned loci.
- Never paraphrase a candidate; a locus is a pointer, not a quote.
- Never write a title ending in a question mark.
- Never duplicate a live locus's bridge in spirit.
- If nothing meaningful needs to change, return actions=[].
"""


_CRITIQUE_INSTRUCTIONS = """\
You are critiquing your own previous output. You will see:
- The user's current task and pinned interpretations.
- A numbered list of Actions the synthesis stage proposed.

A LOCUS OF THOUGHT is a routing pointer, not a summary. Critique each create
action against this standard.

DROP if any of:
- The locus paraphrases a source instead of pointing at a specific part of it.
  source_anchor_md must name a section / function / quote / line range — not
  describe the document as a whole.
- relation_md or overlap_md is generic ("This is important", "Both deal with
  graphs"). The bridge must be concrete and project-specific.
- The locus duplicates a pinned locus's bridge in spirit.
- The links are wrong: a raw appears as src; a derives_from would close a
  cycle; a cites edge points interp→interp; a derives_from points to a raw.
- A reinforce/soften/link targets a handle that doesn't fit this task.
- The action adds noise rather than routing signal.

KEEP if the locus genuinely points at *which part* of the source matters and
*why* for this project, in language the user would recognise as their own.

Output keep[] and drop[] as integer indices into the actions list, plus a
one-line `notes` summary of why.
"""


def _critique_prompt(ctx: _Context, actions: list[Action]) -> str:
    parts = [
        f"USER'S TASK:\n{ctx.instruction}\n",
        f"VALID CANDIDATE HANDLES: {sorted(ctx.candidate_handle_to_id.keys())}\n",
        f"PINNED INTERPRETATIONS: {len(ctx.pinned_node_ids)} (bodies were in synthesis prompt)\n",
        "PROPOSED ACTIONS:\n",
    ]
    for i, a in enumerate(actions):
        parts.append(_render_action(i, a))
    return "\n".join(parts)


def _render_action(i: int, a: Action) -> str:
    head = f"[{i}] action={a.action}"
    if a.action == "create":
        angle_str = f" angle={a.angle}" if a.angle else ""
        link_str = ", ".join(
            f"{lk.src_handle}--{lk.type}-->{lk.dst_handle}" for lk in a.links
        ) or "(none)"
        return (
            f"{head} subkind={a.subkind}{angle_str} title={a.title!r}\n"
            f"      relation={(a.relation_md or '')[:240]!r}\n"
            f"      overlap={(a.overlap_md or '')[:240]!r}\n"
            f"      source_anchor={(a.source_anchor_md or '')[:240]!r}\n"
            f"      links=[{link_str}]\n"
            f"      reason={a.reason!r}"
        )
    head += f" target_handle={a.target_handle}"
    if a.action == "update_angle":
        head += f" angle={a.angle}"
    if a.links:
        link_str = ", ".join(f"{lk.src_handle}--{lk.type}-->{lk.dst_handle}" for lk in a.links)
        head += f" links=[{link_str}]"
    return f"{head} reason={a.reason!r}"
