"""Contradiction pass.

PLAN.md §Edge cases (4): for every raw added since the last absorb, retrieve
the top-3 interpretations, and run a 3-way classifier (contradicts /
reinforces / not_touch). Contradictions file `tension` proposals;
reinforcements bump weight on existing `cites` edges; not-touch is dropped.

Implementation uses a pydantic-ai Agent with `output_type=Verdict`, so the
LLM is forced to return a typed enum — no fragile string parsing. The model
is read from `Settings.classifier_model`; default is the cheap-and-fast
Anthropic Haiku, but the user can point it at any provider.

If no provider is configured we skip the pass (returning a `skipped` summary).
The absorb still runs all its other passes.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Literal

from pydantic import BaseModel

from loci.config import get_settings
from loci.graph.models import new_id
from loci.jobs.proposals import _insert_proposal
from loci.llm import LLMNotConfiguredError, build_agent
from loci.retrieve.vec import search_vec

log = logging.getLogger(__name__)

# Threshold below which we don't even bother classifying — too far apart.
# Distance² = 2*(1 - cos), so 0.7 cosine ↔ distance ~ 0.77.
SIM_FLOOR_DIST = 0.95  # cosine ~ 0.55


class Verdict(BaseModel):
    """Structured output the classifier Agent must return."""
    label: Literal["contradicts", "reinforces", "not_touch"]


CLASSIFIER_INSTRUCTIONS = (
    "You will be given two text excerpts. A is from a *raw source* the user "
    "has read; B is one of the user's prior interpretation notes. Decide the "
    "relationship between A and B and return a Verdict with one of these "
    "labels:\n"
    "  - contradicts: A asserts something that, if true, would make B "
    "substantially wrong.\n"
    "  - reinforces:  A supplies new evidence consistent with B.\n"
    "  - not_touch:   A and B are about different enough things that there is "
    "no clear contradiction or reinforcement.\n"
    "Return only the structured Verdict; no commentary."
)


def run_pass(
    conn: sqlite3.Connection, project_id: str, *, since_iso: str | None = None,
) -> dict:
    """Run the contradiction pass for raws added since `since_iso`.

    Returns a summary dict. Never raises on classifier failure — those are
    logged and recorded as `errors` in the result.
    """
    settings = get_settings()
    try:
        agent = build_agent(
            settings.classifier_model,
            instructions=CLASSIFIER_INSTRUCTIONS,
            output_type=Verdict,
        )
    except LLMNotConfiguredError as exc:
        log.info("contradiction: %s; skipping pass", exc)
        return {"skipped": True, "reason": str(exc)}

    raws = _new_raws(conn, project_id, since_iso)
    if not raws:
        return {"new_raws": 0, "classified": 0, "tensions": 0, "reinforcements": 0}

    tensions = 0
    reinforcements = 0
    classified = 0
    errors: list[str] = []

    for raw in raws:
        emb_row = conn.execute(
            "SELECT embedding FROM node_vec WHERE node_id = ?", (raw["id"],),
        ).fetchone()
        if emb_row is None:
            continue
        # Top-k vec hits, then filter to interp nodes (vec doesn't filter by kind).
        hits = search_vec(
            conn, project_id, _blob_to_np(emb_row["embedding"]),
            k=10, include_status=("live", "dirty"),
        )
        interp_hits = []
        for h in hits:
            if h.distance > SIM_FLOOR_DIST:
                break
            kind = conn.execute(
                "SELECT kind FROM nodes WHERE id = ?", (h.node_id,)
            ).fetchone()["kind"]
            if kind == "interpretation":
                interp_hits.append(h)
            if len(interp_hits) >= 3:
                break
        if not interp_hits:
            continue
        for h in interp_hits:
            try:
                verdict = _classify(agent, raw, h.node_id, conn)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{raw['id']} × {h.node_id}: {exc}")
                continue
            classified += 1
            if verdict == "contradicts":
                payload = {
                    "about_node_id": h.node_id, "evidence_node_ids": [raw["id"]],
                    "reason": "absorb-time contradiction classifier",
                }
                if _insert_proposal(conn, project_id, "tension", payload):
                    tensions += 1
            elif verdict == "reinforces":
                # Bump weight on existing cites edge if present, otherwise
                # add a low-weight one (lets the user accept later).
                existing = conn.execute(
                    "SELECT id, weight FROM edges WHERE src = ? AND dst = ? AND type = 'cites'",
                    (h.node_id, raw["id"]),
                ).fetchone()
                if existing:
                    new_w = min(1.0, existing["weight"] + 0.1)
                    conn.execute(
                        "UPDATE edges SET weight = ? WHERE id = ?",
                        (new_w, existing["id"]),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO edges(id, src, dst, type, weight, created_by)
                        VALUES (?, ?, ?, 'cites', 0.4, 'system')
                        """,
                        (new_id(), h.node_id, raw["id"]),
                    )
                reinforcements += 1
            # not_touch → drop
    return {
        "new_raws": len(raws), "classified": classified,
        "tensions": tensions, "reinforcements": reinforcements,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _new_raws(conn, project_id: str, since_iso: str | None) -> list:
    sql = """
        SELECT n.id AS id, n.title, n.body, n.created_at
        FROM nodes n
        JOIN raw_nodes r ON r.node_id = n.id
        JOIN project_effective_members pm ON pm.node_id = n.id
        WHERE pm.project_id = ?
          AND r.source_of_truth = 1
    """
    params = [project_id]
    if since_iso:
        sql += " AND n.created_at > ?"
        params.append(since_iso)
    sql += " ORDER BY n.created_at"
    return conn.execute(sql, tuple(params)).fetchall()


def _classify(agent, raw_row, interp_id: str, conn) -> str:
    raw_excerpt = (raw_row["body"] or "").strip()[:1500]
    interp_row = conn.execute(
        "SELECT title, body FROM nodes WHERE id = ?", (interp_id,),
    ).fetchone()
    interp_excerpt = (
        f"{interp_row['title']}\n\n{(interp_row['body'] or '').strip()[:800]}"
    )
    user_msg = (
        f"A (raw):\n{raw_excerpt}\n\n---\n\nB (interpretation):\n{interp_excerpt}"
    )
    result = agent.run_sync(user_msg)
    return result.output.label  # Verdict.label


def _blob_to_np(blob: bytes):
    """Decode a sqlite-vec blob back to a numpy array."""
    import numpy as np
    return np.frombuffer(blob, dtype=np.float32).copy()
