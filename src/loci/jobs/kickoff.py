"""Kickoff job — generate the first interpretation seeds for a new project.

Kickoff writes `tension` nodes directly to live state at conservative confidence
(`confidence=0.5`, `origin=agent_synthesis`). Tension nodes represent open questions
and unresolved conflicts — they assert nothing but invite the user's reasoning.
Subsequent drafting and corrections will evolve them into decisions, philosophies,
and relevance interpretations.

Without an LLM configured, kickoff returns `skipped: true` with no node writes.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Annotated

from pydantic import BaseModel, Field

from loci.citations import CitationTracker
from loci.config import get_settings
from loci.graph.models import InterpretationNode
from loci.graph.nodes import NodeRepository
from loci.graph.projects import ProjectRepository
from loci.llm import LLMNotConfiguredError, build_agent

log = logging.getLogger(__name__)

# How many raw-node titles + leading text to include as "what the user has
# actually read so far". Too few → questions are abstract; too many → token
# cost balloons.
RAW_SAMPLE_COUNT = 12
RAW_EXCERPT_CHARS = 500
TARGET_QUESTIONS = 8


KICKOFF_INSTRUCTIONS = (
    "You are helping a user start a new research/writing project. They will "
    "give you a PROJECT PROFILE (their stated goals, scope, and taste) and a "
    "SAMPLE of source material they have collected. Your job is to propose "
    "{n} open TENSIONS worth pursuing within this project.\n\n"
    "Rules — follow these strictly:\n"
    "- Output a JSON-shaped Kickoff with a `questions` field; each tension "
    "  has `title` (≤80 chars, phrased as a question or conflict) and `body` "
    "  (1–3 sentences explaining why this tension matters and what resolving "
    "  it would unlock).\n"
    "- Do NOT make claims or pretend to know the user's interpretation. "
    "  Frame as open tensions: unresolved questions, competing priorities, "
    "  or gaps between what the sources show and what the project needs.\n"
    "- Stay tight to the profile. Ignore directions outside the scope.\n"
    "- Phrase in the user's voice — first person, casual."
)


class KickoffQuestion(BaseModel):
    title: Annotated[str, Field(max_length=200)]
    body: str


class Kickoff(BaseModel):
    questions: list[KickoffQuestion]


def run(conn: sqlite3.Connection, project_id: str | None, payload: dict) -> dict:
    """Kickoff handler. Signature matches the worker dispatch convention."""
    if project_id is None:
        raise ValueError("kickoff requires a project_id")

    project = ProjectRepository(conn).get(project_id)
    if project is None:
        raise ValueError(f"project not found: {project_id}")

    target = int(payload.get("n", TARGET_QUESTIONS))
    target = max(3, min(20, target))  # clamp to sane bounds

    settings = get_settings()
    try:
        agent = build_agent(
            settings.interpretation_model,
            instructions=KICKOFF_INSTRUCTIONS.format(n=target),
            output_type=Kickoff,
        )
    except LLMNotConfiguredError as exc:
        log.info("kickoff: %s; skipping LLM step", exc)
        return {"skipped": True, "reason": str(exc), "proposals": 0}

    sample = _sample_raws(conn, project_id, RAW_SAMPLE_COUNT, RAW_EXCERPT_CHARS)
    if not sample.strip() and not project.profile_md.strip():
        return {"skipped": True, "reason": "no profile, no raws to sample", "proposals": 0}

    user_msg = (
        f"PROJECT PROFILE:\n{project.profile_md or '(empty — infer from sample)'}"
        f"\n\n---\n\nSAMPLE OF RAW SOURCES:\n{sample}"
    )

    try:
        result = agent.run_sync(user_msg)
    except Exception as exc:  # noqa: BLE001
        log.exception("kickoff: LLM call failed")
        return {"skipped": False, "error": str(exc), "proposals": 0}

    kickoff: Kickoff = result.output
    nodes_repo = NodeRepository(conn)
    pr = ProjectRepository(conn)
    tracker = CitationTracker(conn)

    # Embed the questions in one batch so we pay the embedder cost once.
    from loci.embed.local import get_embedder
    embedder = None
    try:
        embedder = get_embedder()
    except Exception as exc:  # noqa: BLE001
        log.warning("kickoff: embedder load failed: %s", exc)
    questions = kickoff.questions[:target]
    vecs = None
    if embedder is not None and questions:
        try:
            vecs = embedder.encode_batch([f"{q.title}\n\n{q.body}" for q in questions])
        except Exception as exc:  # noqa: BLE001
            log.warning("kickoff: embedding failed: %s", exc)
            vecs = None

    written = 0
    for i, q in enumerate(questions):
        try:
            node = InterpretationNode(
                subkind="tension",
                title=q.title.strip(),
                body=q.body.strip(),
                origin="agent_synthesis",
                confidence=0.5,        # open questions are speculative, not asserted
                status="live",         # but live — they show up in retrieval
            )
            embedding = vecs[i] if vecs is not None else None
            nodes_repo.create_interpretation(node, embedding=embedding)
            pr.add_member(project_id, node.id, role="included", added_by="agent")
            tracker.append_trace(
                project_id, node.id, "agent_synthesised", client="kickoff",
            )
            written += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("kickoff: write tension failed: %s", exc)

    # Anchor wiring + co-citation — runs after all nodes are written so new
    # question nodes appear in retrieval immediately and share evidence edges.
    if written > 0 and embedder is not None:
        try:
            _anchor_and_cocite(conn, project_id, embedder)
        except Exception as exc:  # noqa: BLE001
            log.warning("kickoff: anchor wiring failed: %s", exc)

    return {
        "skipped": False,
        "tensions_written": written,
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
    import struct
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
        raws = conn.execute("""
            SELECT n.id, nv.embedding FROM nodes n
            JOIN node_vec nv ON nv.node_id = n.id
            WHERE n.kind = 'raw' AND n.status = 'live'
              AND n.id IN (SELECT node_id FROM project_effective_members WHERE project_id = ?)
        """, (project_id,)).fetchall()

        def _decode(b: bytes) -> "np.ndarray":
            n = len(b) // 4
            return np.array(struct.unpack(f"{n}f", b), dtype=np.float32)

        raw_embs = [(r[0], _decode(r[1])) for r in raws]

        for node in isolated:
            node_id, title, body = node[0], node[1], node[2]
            emb = np.array(embedder.encode(body or title), dtype=np.float32)
            sims = sorted(
                [(rid, float(np.dot(emb, remb))) for rid, remb in raw_embs],
                key=lambda x: -x[1],
            )
            for raw_id, sim in sims[:3]:
                if not conn.execute(
                    "SELECT 1 FROM edges WHERE src=? AND dst=? AND type='cites'",
                    (node_id, raw_id),
                ).fetchone():
                    conn.execute(
                        "INSERT INTO edges(id, src, dst, type, weight, created_by, created_at)"
                        " VALUES (?,?,?,?,?,?,datetime('now'))",
                        (new_id(), node_id, raw_id, "cites", round(sim, 3), "system"),
                    )

    # Co-citation: interp pairs that cite the same raw
    pairs = conn.execute("""
        SELECT DISTINCT e1.src AS a, e2.src AS b
        FROM edges e1 JOIN edges e2 ON e1.dst = e2.dst AND e1.src < e2.src
        WHERE e1.type = 'cites' AND e2.type = 'cites'
          AND e1.src IN (SELECT node_id FROM interpretation_nodes)
          AND e2.src IN (SELECT node_id FROM interpretation_nodes)
          AND e1.src IN (SELECT node_id FROM project_effective_members WHERE project_id = ?)
    """, (project_id,)).fetchall()

    for pair in pairs:
        a, b = pair[0], pair[1]
        if not conn.execute(
            "SELECT 1 FROM edges WHERE src=? AND dst=? AND type='semantic'", (a, b)
        ).fetchone():
            conn.execute(
                "INSERT INTO edges(id, src, dst, type, weight, created_by, created_at)"
                " VALUES (?,?,?,?,?,?,datetime('now'))",
                (new_id(), a, b, "semantic", 1.0, "system"),
            )

    conn.commit()


def _sample_raws(conn: sqlite3.Connection, project_id: str, n: int, excerpt_chars: int) -> str:
    """Return a stringified sample of raws for the kickoff prompt.

    We pick the most recently added raws (proxy for "what the user is currently
    working with"). Each entry includes the title + the first `excerpt_chars`
    characters of the body — enough for the LLM to recognise the topic without
    blowing token budget.
    """
    rows = conn.execute(
        """
        SELECT n.title, n.body, n.subkind
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
    for r in rows:
        body = (r["body"] or "").strip()[:excerpt_chars]
        if body:
            parts.append(f"- [{r['subkind']}] {r['title']}\n  {body}")
    return "\n\n".join(parts)
