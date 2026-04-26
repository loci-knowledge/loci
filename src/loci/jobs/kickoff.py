"""Kickoff job — generate the first loci of thought for a new project.

A locus is a *pointer*, not a summary. It says: "here is where this source's
content meets this project's intent, and here is which part of the source
carries the weight." Loci come in four framings (subkinds): philosophy,
tension, decision, relevance.

Kickoff writes loci at conservative confidence (0.5, origin=agent_synthesis,
status=live). Each locus must include the three slots:

  - relation_md       (1–3 sentences: how source relates to project)
  - overlap_md        (the concrete intersection — what they share)
  - source_anchor_md  (which part of which source: section, function, line,
                       quote — never a paraphrase of the whole document)

Without an LLM configured, kickoff returns `skipped: true` with no node writes.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Annotated

from pydantic import BaseModel, Field

from loci.citations import CitationTracker
from loci.config import get_settings
from loci.graph import EdgeRepository
from loci.graph.models import InterpretationNode, RelevanceAngle
from loci.graph.nodes import NodeRepository
from loci.graph.projects import ProjectRepository
from loci.graph.workspaces import WorkspaceRepository
from loci.llm import LLMNotConfiguredError, build_agent

log = logging.getLogger(__name__)

# How many raw-node excerpts to include as sample.
RAW_SAMPLE_COUNT = 12
RAW_EXCERPT_CHARS = 500
TARGET_OBSERVATIONS = 6


KICKOFF_INSTRUCTIONS = """\
You are seeding the user's loci-of-thought graph for a new project.

A LOCUS OF THOUGHT is NOT a summary. It is a pointer: a place in concept-space
that says "the part of THIS source over here meets the part of THIS project
over there, in this specific way." Loci are how the user (and later, retrieval)
finds their way back to the right paragraph of the right source. The body of a
locus is never the answer — the source is the answer, and the locus tells you
which part to read and why.

You are given:
  - PROJECT PROFILE: the user's goals, scope, and taste for this project.
  - LINKED WORKSPACES: kinds and labels of the information sources connected.
  - WORKSPACE SOURCES: titled excerpts labelled [R1], [R2], … from the project's
    sources. Each excerpt is a *fragment*; assume the full source has more.

Generate {n} loci. Each locus has THREE required slots and a subkind framing:

  relation_md       — 1–3 sentences. How does this source RELATE to this project?
                      Name the bridge in concrete terms. Do not paraphrase the
                      source; describe the relationship.
  overlap_md        — 1–2 sentences. WHERE do they overlap? Be specific:
                      "both build a personal knowledge graph over local files",
                      not "both deal with knowledge."
  source_anchor_md  — Which PART of the source carries the weight? Quote a
                      phrase, name a section, point at a function, cite a line
                      range, identify a definition. NOT a summary of the source.
                      If multiple sources, anchor each one separately.

Subkinds (pick the one that genuinely fits — do not default to relevance):

  - `relevance`: a typed bridge between specific source(s) and the project's
    intent. REQUIRES: `angle` ∈ {{applicable_pattern, experimental_setup,
    borrowed_concept, counterexample, prior_attempt, vocabulary_source,
    methodological_neighbor, contrast_baseline}}. Cite ≥2 raws via raw_handles.

  - `philosophy`: a first-principle belief the sources reveal the project
    should hold. The relation_md says HOW this principle shows up in the
    sources; source_anchor_md points at where the principle is stated/embodied.

  - `tension`: an unresolved conflict between source(s) and project — two
    values pulling against each other. relation_md names the tension precisely;
    source_anchor_md points at where each side is visible.

  - `decision`: a concrete choice the sources suggest the project should make,
    with explicit trade-offs. relation_md names the decision; source_anchor_md
    points at the evidence informing each side of the trade-off.

Rules:
  - title (≤80 chars) is the LOCUS NAME — short, evocative, useful in a list.
    "CLI server pattern bridges loki-frontend and loci-backend" not
    "An analysis of CLI server architecture."
  - Never write a locus title that ends in a question mark.
  - raw_handles must reference [Rn] labels present in the sample.
  - Cite ≥2 raws when possible — a locus that touches only one source is fine,
    but two-source loci surface bridges that single-source ones miss.
  - body MAY be empty; the three slots above carry the meaning. If you write
    a body, keep it short (≤300 chars) — additional context, not summary.
  - Stay grounded in the project profile + the actual sources sampled.
"""


class KickoffObservation(BaseModel):
    subkind: str  # "relevance" | "philosophy" | "tension" | "decision"
    title: Annotated[str, Field(max_length=200)]
    body: str = ""
    relation_md: str
    overlap_md: str
    source_anchor_md: str
    angle: RelevanceAngle | None = None
    raw_handles: list[str] = Field(default_factory=list)


class KickoffOutput(BaseModel):
    observations: list[KickoffObservation]


def run(conn: sqlite3.Connection, project_id: str | None, payload: dict) -> dict:
    """Kickoff handler. Signature matches the worker dispatch convention."""
    if project_id is None:
        raise ValueError("kickoff requires a project_id")

    project = ProjectRepository(conn).get(project_id)
    if project is None:
        raise ValueError(f"project not found: {project_id}")

    target = int(payload.get("n", TARGET_OBSERVATIONS))
    target = max(2, min(12, target))  # clamp to sane bounds

    settings = get_settings()
    try:
        agent = build_agent(
            settings.interpretation_model,
            instructions=KICKOFF_INSTRUCTIONS.format(n=target),
            output_type=KickoffOutput,
        )
    except LLMNotConfiguredError as exc:
        log.info("kickoff: %s; skipping LLM step", exc)
        return {"skipped": True, "reason": str(exc), "proposals": 0}

    sample_str, handle_to_id = _sample_raws_with_handles(
        conn, project_id, RAW_SAMPLE_COUNT, RAW_EXCERPT_CHARS
    )
    workspace_summary = _workspace_summary(conn, project_id)

    if not sample_str.strip() and not project.profile_md.strip():
        return {"skipped": True, "reason": "no profile, no raws to sample", "proposals": 0}

    user_msg_parts = [
        f"PROJECT PROFILE:\n{project.profile_md or '(empty — infer from sample)'}",
    ]
    if workspace_summary:
        user_msg_parts.append(f"LINKED WORKSPACES:\n{workspace_summary}")
    if sample_str:
        user_msg_parts.append(f"WORKSPACE SOURCES:\n{sample_str}")

    user_msg = "\n\n---\n\n".join(user_msg_parts)

    try:
        result = agent.run_sync(user_msg)
    except Exception as exc:  # noqa: BLE001
        log.exception("kickoff: LLM call failed")
        return {"skipped": False, "error": str(exc), "proposals": 0}

    output: KickoffOutput = result.output
    nodes_repo = NodeRepository(conn)
    pr = ProjectRepository(conn)
    edges_repo = EdgeRepository(conn)
    tracker = CitationTracker(conn)

    # Embed all observations in one batch.
    from loci.embed.local import get_embedder
    embedder = None
    try:
        embedder = get_embedder()
    except Exception as exc:  # noqa: BLE001
        log.warning("kickoff: embedder load failed: %s", exc)

    observations = output.observations[:target]
    vecs = None
    if embedder is not None and observations:
        try:
            # Encode the locus's three slots (relation + overlap + anchor) plus
            # title — that's the routing signature we want vec/lex retrieval
            # to score interpretation handles against.
            vecs = embedder.encode_batch([
                "\n\n".join(part for part in [
                    o.title, o.relation_md, o.overlap_md, o.source_anchor_md,
                ] if part)
                for o in observations
            ])
        except Exception as exc:  # noqa: BLE001
            log.warning("kickoff: embedding failed: %s", exc)
            vecs = None

    written = 0
    for i, obs in enumerate(observations):
        try:
            node = InterpretationNode(
                subkind=obs.subkind,  # type: ignore[arg-type]
                title=obs.title.strip(),
                body=(obs.body or "").strip(),
                relation_md=obs.relation_md.strip(),
                overlap_md=obs.overlap_md.strip(),
                source_anchor_md=obs.source_anchor_md.strip(),
                angle=obs.angle,
                rationale_md="",  # legacy; the three slots replace it
                origin="agent_synthesis",
                confidence=0.5,
                status="live",
            )
            embedding = vecs[i] if vecs is not None else None
            nodes_repo.create_interpretation(node, embedding=embedding)
            # Wire cites edges — moved up so we always attach the locus to its
            # anchored sources before counting it as written.
            pr.add_member(project_id, node.id, role="included", added_by="agent")
            tracker.append_trace(
                project_id, node.id, "agent_synthesised", client="kickoff",
            )

            # Wire cites edges to referenced raw nodes.
            for handle in obs.raw_handles:
                raw_id = handle_to_id.get(handle.upper().lstrip("R").zfill(0))
                # Try both "R1" and "1" forms.
                if raw_id is None:
                    # handle might be "R1" → strip "R" prefix
                    key = handle.upper()
                    if key.startswith("R"):
                        key = key[1:]
                    raw_id = handle_to_id.get(key)
                if raw_id is None:
                    # direct lookup with the handle as-is
                    raw_id = handle_to_id.get(handle)
                if raw_id is not None:
                    try:
                        edges_repo.create(node.id, raw_id, type="cites", created_by="system")
                    except Exception as exc:  # noqa: BLE001
                        log.warning("kickoff: cites edge failed: %s", exc)

            written += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("kickoff: write observation failed: %s", exc)

    # Anchor wiring — runs after all nodes are written. Attaches loci with no
    # cites edges to their nearest raws so they aren't orphans.
    if written > 0 and embedder is not None:
        try:
            _anchor_isolated(conn, project_id, embedder)
        except Exception as exc:  # noqa: BLE001
            log.warning("kickoff: anchor wiring failed: %s", exc)

    return {
        "skipped": False,
        "observations_written": written,
        "model": settings.interpretation_model,
    }


def _anchor_isolated(
    conn: sqlite3.Connection,
    project_id: str,
    embedder,
) -> None:
    """Wire isolated interpretation nodes (no cites edge yet) to nearest raws.

    A locus with no `cites` edges is a locus with no source — useless for
    routing. After kickoff we sweep these and attach each to its top-3 nearest
    raws by cosine similarity, weighting by the similarity. Better than nothing
    until the agent or user produces a more deliberate anchor.

    Co-citation / semantic edges are NOT generated — those were the old
    symmetric edges that made the graph cyclic. Shared evidence is now
    expressed by overlapping `cites` fan-outs from each locus, and any
    locus-to-locus relationship goes through `derives_from` (added by the
    interpreter agent, not by post-hoc co-citation).
    """
    import numpy as np
    from loci.graph.models import new_id

    isolated = conn.execute("""
        SELECT n.id, n.title, n.body FROM nodes n
        JOIN interpretation_nodes i ON i.node_id = n.id
        WHERE n.id NOT IN (SELECT src FROM edges WHERE type = 'cites')
          AND n.status = 'live'
          AND n.id IN (
              SELECT node_id FROM project_effective_members WHERE project_id = ?
          )
    """, (project_id,)).fetchall()

    if not isolated:
        return

    from loci.embed.local import blob_to_vec

    raws = conn.execute("""
        SELECT n.id, nv.embedding FROM nodes n
        JOIN node_vec nv ON nv.node_id = n.id
        WHERE n.kind = 'raw' AND n.status = 'live'
          AND n.id IN (SELECT node_id FROM project_effective_members WHERE project_id = ?)
    """, (project_id,)).fetchall()

    raw_embs = [(r[0], blob_to_vec(r[1], len(r[1]) // 4)) for r in raws]
    if not raw_embs:
        return

    isolated_ids = [n[0] for n in isolated]
    ph = ",".join("?" * len(isolated_ids))
    existing_cites: set[tuple[str, str]] = {
        (row[0], row[1]) for row in conn.execute(
            f"SELECT src, dst FROM edges WHERE type='cites' AND src IN ({ph})",
            tuple(isolated_ids),
        ).fetchall()
    }

    for node in isolated:
        node_id, title, body = node[0], node[1], node[2]
        emb = np.array(embedder.encode(body or title), dtype=np.float32)
        sims = sorted(
            [(rid, float(np.dot(emb, remb))) for rid, remb in raw_embs],
            key=lambda x: -x[1],
        )
        for raw_id, sim in sims[:3]:
            if (node_id, raw_id) not in existing_cites:
                conn.execute(
                    "INSERT INTO edges(id, src, dst, type, weight, created_by, created_at)"
                    " VALUES (?,?,?,?,?,?,datetime('now'))",
                    (new_id(), node_id, raw_id, "cites", round(sim, 3), "system"),
                )
                existing_cites.add((node_id, raw_id))

    conn.commit()


def _sample_raws_with_handles(
    conn: sqlite3.Connection,
    project_id: str,
    n: int,
    excerpt_chars: int,
) -> tuple[str, dict[str, str]]:
    """Return a formatted sample string with [R1]..[Rn] labels AND a dict mapping
    numeric key (e.g. '1') → raw_node_id.

    Query joins nodes + raw_nodes + project_effective_members, ordered by
    created_at DESC LIMIT n.
    """
    rows = conn.execute(
        """
        SELECT n.id, n.title, n.body, n.subkind
        FROM nodes n
        JOIN raw_nodes r ON r.node_id = n.id
        JOIN project_effective_members pm ON pm.node_id = n.id
        WHERE pm.project_id = ?
          AND r.source_of_truth = 1
        ORDER BY n.created_at DESC
        LIMIT ?
        """,
        (project_id, n),
    ).fetchall()

    parts: list[str] = []
    handle_to_id: dict[str, str] = {}

    for idx, row in enumerate(rows, start=1):
        key = str(idx)
        handle_to_id[key] = row["id"]
        body = (row["body"] or "").strip()[:excerpt_chars]
        label = f"[R{idx}]"
        header = f"{label} [{row['subkind']}] {row['title']}"
        if body:
            parts.append(f"{header}\n  {body}")
        else:
            parts.append(header)

    return "\n\n".join(parts), handle_to_id


def _workspace_summary(conn: sqlite3.Connection, project_id: str) -> str:
    """List linked workspace names/kinds for context header."""
    ws_repo = WorkspaceRepository(conn)
    links = ws_repo.linked_workspaces(project_id)
    if not links:
        return ""
    lines: list[str] = []
    for ws, link in links:
        if link.role == "excluded":
            continue
        desc = f" — {ws.description_md[:100]}" if ws.description_md else ""
        lines.append(f"- [{ws.kind}] {ws.name} (role={link.role}){desc}")
    return "\n".join(lines)
