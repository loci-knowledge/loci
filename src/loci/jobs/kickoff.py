"""Kickoff job — generate relationship observations for a new project.

Kickoff writes `relevance`, `philosophy`, and `decision` interpretation nodes
that capture HOW workspace sources connect to the project's goals. Nodes land
at conservative confidence (`confidence=0.5`, `origin=agent_synthesis`, `status=live`).

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
You are helping a user start a new research/writing project. They have given you a \
PROJECT PROFILE (their goals, scope, and taste) and WORKSPACE SOURCES (titled excerpts \
labeled [R1], [R2], etc.). Generate {n} relationship observations that capture how this \
workspace content connects to the project's goals.

Each observation must be one of three subkinds:
- `relevance`: a typed bridge between specific workspace sources and the project's intent. \
  Requires: angle (one of applicable_pattern, experimental_setup, borrowed_concept, \
  counterexample, prior_attempt, vocabulary_source, methodological_neighbor, \
  contrast_baseline), rationale_md (1-3 sentences: WHY these specific sources matter at \
  this angle). Cite ≥2 raws via raw_handles (e.g. ["R1", "R3"]). Name the bridge — \
  do not summarise the sources.
- `philosophy`: a first-principle belief the sources reveal the project should adopt. \
  No angle required.
- `decision`: a concrete choice the sources suggest the project should make, with \
  explicit trade-offs named.

Rules:
- Output OBSERVATIONS, not questions. Never use question marks in titles.
- For relevance nodes: name what the sources OFFER for the project, not what they say.
- raw_handles must reference actual [Rn] labels present in the sample.
- Generate a mix of subkinds; lean toward relevance when the workspace has distinct sources.
- Stay grounded in the project profile and the sources given.
"""


class KickoffObservation(BaseModel):
    subkind: str  # "relevance" | "philosophy" | "decision"
    title: Annotated[str, Field(max_length=200)]
    body: str
    angle: RelevanceAngle | None = None
    rationale_md: str | None = None
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
            vecs = embedder.encode_batch(
                [f"{o.title}\n\n{o.body}" for o in observations]
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("kickoff: embedding failed: %s", exc)
            vecs = None

    written = 0
    for i, obs in enumerate(observations):
        try:
            node = InterpretationNode(
                subkind=obs.subkind,  # type: ignore[arg-type]
                title=obs.title.strip(),
                body=obs.body.strip(),
                angle=obs.angle,
                rationale_md=obs.rationale_md or "",
                origin="agent_synthesis",
                confidence=0.5,
                status="live",
            )
            embedding = vecs[i] if vecs is not None else None
            nodes_repo.create_interpretation(node, embedding=embedding)
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

    # Anchor wiring + co-citation — runs after all nodes are written.
    if written > 0 and embedder is not None:
        try:
            _anchor_and_cocite(conn, project_id, embedder)
        except Exception as exc:  # noqa: BLE001
            log.warning("kickoff: anchor wiring failed: %s", exc)

    return {
        "skipped": False,
        "observations_written": written,
        "model": settings.interpretation_model,
    }


def _anchor_and_cocite(
    conn: sqlite3.Connection,
    project_id: str,
    embedder,
) -> None:
    """Wire isolated interpretation nodes to nearest raw nodes and build semantic edges.

    Isolated interp nodes (no `cites` edge yet) get wired to their 3 nearest raws
    via cosine similarity. Then pairs of interp nodes that cite the same raw get a
    `semantic` edge to signal shared evidence.
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

    if isolated:
        from loci.embed.local import blob_to_vec

        raws = conn.execute("""
            SELECT n.id, nv.embedding FROM nodes n
            JOIN node_vec nv ON nv.node_id = n.id
            WHERE n.kind = 'raw' AND n.status = 'live'
              AND n.id IN (SELECT node_id FROM project_effective_members WHERE project_id = ?)
        """, (project_id,)).fetchall()

        raw_embs = [(r[0], blob_to_vec(r[1], len(r[1]) // 4)) for r in raws]

        # Pre-fetch existing cites edges for these nodes to avoid per-row queries.
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

    # Co-citation: interp pairs that cite the same raw
    pairs = conn.execute("""
        SELECT DISTINCT e1.src AS a, e2.src AS b
        FROM edges e1 JOIN edges e2 ON e1.dst = e2.dst AND e1.src < e2.src
        WHERE e1.type = 'cites' AND e2.type = 'cites'
          AND e1.src IN (SELECT node_id FROM interpretation_nodes)
          AND e2.src IN (SELECT node_id FROM interpretation_nodes)
          AND e1.src IN (SELECT node_id FROM project_effective_members WHERE project_id = ?)
    """, (project_id,)).fetchall()

    if pairs:
        # Pre-fetch existing semantic edges to avoid per-pair queries.
        pair_srcs = list({p[0] for p in pairs})
        ph2 = ",".join("?" * len(pair_srcs))
        existing_semantic: set[tuple[str, str]] = {
            (row[0], row[1]) for row in conn.execute(
                f"SELECT src, dst FROM edges WHERE type='semantic' AND src IN ({ph2})",
                tuple(pair_srcs),
            ).fetchall()
        }
        for pair in pairs:
            a, b = pair[0], pair[1]
            if (a, b) not in existing_semantic:
                conn.execute(
                    "INSERT INTO edges(id, src, dst, type, weight, created_by, created_at)"
                    " VALUES (?,?,?,?,?,?,datetime('now'))",
                    (new_id(), a, b, "semantic", 1.0, "system"),
                )
                existing_semantic.add((a, b))

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
