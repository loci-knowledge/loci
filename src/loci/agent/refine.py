"""Rubric-aligned draft refinement.

Implements a Self-Refine loop: score the draft against a citation rubric,
generate a critique, rewrite, re-verify. Capped at max_iter iterations.
Each iteration is logged to agent_reflections with trigger='draft_refine'.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from loci.config import get_settings
from loci.draft import _CITE_RE
from loci.graph.models import new_id
from loci.llm import LLMNotConfiguredError, build_agent
from loci.verify import CitationVerdict, verify

log = logging.getLogger(__name__)

CITATION_RUBRIC = """
Citation quality rubric — score each dimension 0-3:
1. Coverage: every substantive claim has at least one [Cn] citation
2. Grounding: cited chunks actually support the claims they are attached to
3. Economy: no orphan citations ([Cn] appears but the handle maps to no candidate)
4. Precision: no over-citation (≥4 citations on a single sentence)
Overall score = (sum of dimensions) / 12. Threshold for passing: 0.7
"""

_CRITIQUE_SYSTEM = (
    "You are a citation-quality editor. Given a draft and a rubric, identify specific issues "
    "and suggest targeted improvements. Be concise — output a numbered list of issues only."
)

_REWRITE_SYSTEM = (
    "You are a precise academic writer. Rewrite the draft to fix citation issues while "
    "preserving all factual content. Every claim must have exactly one supporting [Cn] "
    "citation from the provided candidates. Do not invent citations."
)


@dataclass
class RefineIter:
    iter_num: int
    score: float
    critique: str
    output_md: str   # the output after this iteration (may equal prior if not improved)
    improved: bool


def refine_draft(
    *,
    conn: sqlite3.Connection,
    project_id: str,
    response_id: str | None,
    instruction: str,
    candidate_block: str,
    output_md: str,
    verdicts: list,           # CitationVerdict list from verify.py
    handle_to_chunk_text: dict[str, str],
    max_iter: int = 2,
    threshold: float = 0.7,
) -> tuple[str, list[RefineIter]]:
    """Run the Self-Refine loop, returning (final_output_md, list_of_iters).

    Each iteration scores the draft against the citation rubric, generates a
    critique, rewrites the draft, re-verifies, and logs to agent_reflections.
    Returns early if the draft already passes or if no improvement is possible.
    """
    # 1. Initial score.
    unsupported_count = sum(1 for v in verdicts if v.verdict == "unsupported")
    score = 1.0 - min(1.0, unsupported_count / max(1, len(verdicts))) if verdicts else 0.5

    # 2. Fast-path: already good enough.
    if score >= threshold and unsupported_count == 0:
        return output_md, []

    settings = get_settings()
    current_output_md = output_md
    current_verdicts = verdicts
    iters: list[RefineIter] = []

    # Build agents once — cheap to construct but wasteful to recreate per iter.
    try:
        critique_agent = build_agent(settings.rag_model, instructions=_CRITIQUE_SYSTEM)
        rewrite_agent = build_agent(settings.rag_model, instructions=_REWRITE_SYSTEM)
    except LLMNotConfiguredError as exc:
        log.warning("refine: LLM not configured, skipping: %s", exc)
        return output_md, []

    for iter_num in range(1, max_iter + 1):
        # ----------------------------------------------------------------
        # a. Build verdicts summary (only problem cases, capped at 10).
        # ----------------------------------------------------------------
        bad_verdicts = [
            v for v in current_verdicts if v.verdict in ("unsupported", "partial")
        ][:10]
        verdicts_summary = "\n".join(
            f"{v.handle}: {v.verdict} — {v.reason}" for v in bad_verdicts
        )

        # ----------------------------------------------------------------
        # b. Critique step.
        # ----------------------------------------------------------------
        critique = ""
        try:
            critique_prompt = (
                f"RUBRIC:\n{CITATION_RUBRIC}\n\n"
                f"VERDICTS (entailment check results):\n{verdicts_summary}\n\n"
                f"DRAFT:\n{current_output_md}\n\n"
                "List the top 3 issues with this draft's citation quality:"
            )
            critique_result = critique_agent.run_sync(critique_prompt)
            critique = (critique_result.output or "").strip()
        except LLMNotConfiguredError as exc:
            log.warning("refine: LLM not configured for critique (iter %d): %s", iter_num, exc)
            _log_reflection(
                conn, project_id, response_id, iter_num,
                f"SKIPPED (LLM not configured): {exc}", "[]",
            )
            return current_output_md, iters
        except Exception as exc:  # noqa: BLE001
            log.warning("refine: critique step failed (iter %d): %s", iter_num, exc)
            _log_reflection(
                conn, project_id, response_id, iter_num,
                f"SKIPPED (critique error): {exc}", "[]",
            )
            return current_output_md, iters

        if not critique:
            log.debug("refine: empty critique at iter %d; stopping", iter_num)
            break

        # ----------------------------------------------------------------
        # c. Rewrite step.
        # ----------------------------------------------------------------
        new_output_md = ""
        try:
            rewrite_prompt = (
                f"ORIGINAL INSTRUCTION:\n{instruction}\n\n"
                f"CITATION ISSUES TO FIX:\n{critique}\n\n"
                f"CANDIDATES (for citation reference):\n{candidate_block}\n\n"
                f"DRAFT TO REVISE:\n{current_output_md}\n\n"
                "Write the improved draft:"
            )
            rewrite_result = rewrite_agent.run_sync(rewrite_prompt)
            new_output_md = (rewrite_result.output or "").strip()
        except LLMNotConfiguredError as exc:
            log.warning("refine: LLM not configured for rewrite (iter %d): %s", iter_num, exc)
            _log_reflection(
                conn, project_id, response_id, iter_num,
                f"SKIPPED (LLM not configured): {exc}", "[]",
            )
            return current_output_md, iters
        except Exception as exc:  # noqa: BLE001
            log.warning("refine: rewrite step failed (iter %d): %s", iter_num, exc)
            _log_reflection(
                conn, project_id, response_id, iter_num,
                f"SKIPPED (rewrite error): {exc}", "[]",
            )
            return current_output_md, iters

        if not new_output_md:
            log.debug("refine: empty rewrite at iter %d; stopping", iter_num)
            break

        # ----------------------------------------------------------------
        # d. Re-verify the rewritten draft.
        # ----------------------------------------------------------------
        new_verdicts: list[CitationVerdict] = []
        try:
            new_handles = {f"C{int(m.group(1))}" for m in _CITE_RE.finditer(new_output_md)}
            chunks_for_verifier = {
                h: handle_to_chunk_text[h]
                for h in new_handles
                if h in handle_to_chunk_text and handle_to_chunk_text[h]
            }
            if chunks_for_verifier:
                vres = verify(new_output_md, chunks_for_verifier)
                new_verdicts = vres.verdicts
        except Exception as exc:  # noqa: BLE001
            log.warning("refine: re-verify failed (iter %d): %s", iter_num, exc)
            new_verdicts = []

        # ----------------------------------------------------------------
        # e. Compute new score and decide whether to accept.
        # ----------------------------------------------------------------
        new_unsupported = sum(1 for v in new_verdicts if v.verdict == "unsupported")
        if new_verdicts:
            new_score = 1.0 - min(1.0, new_unsupported / max(1, len(new_verdicts)))
        else:
            new_score = score  # can't tell → keep current score

        improved = new_score > score
        if improved:
            current_output_md = new_output_md
            current_verdicts = new_verdicts
            score = new_score
            unsupported_count = new_unsupported

        # ----------------------------------------------------------------
        # f. Log to agent_reflections.
        # ----------------------------------------------------------------
        _log_reflection(
            conn, project_id, response_id, iter_num, critique, "[]",
        )

        # ----------------------------------------------------------------
        # g. Record this iteration.
        # ----------------------------------------------------------------
        iters.append(RefineIter(
            iter_num=iter_num,
            score=new_score,
            critique=critique,
            output_md=current_output_md,
            improved=improved,
        ))

        # ----------------------------------------------------------------
        # h. Early exit if quality is now satisfactory.
        # ----------------------------------------------------------------
        if unsupported_count == 0 and score >= threshold:
            log.debug("refine: quality threshold met at iter %d (score=%.3f)", iter_num, score)
            break

    return current_output_md, iters


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log_reflection(
    conn: sqlite3.Connection,
    project_id: str,
    response_id: str,
    iter_num: int,
    deliberation_md: str,
    actions_json: str,
) -> None:
    """Insert one row into agent_reflections for this refinement iteration."""
    try:
        rid = new_id()
        conn.execute(
            """
            INSERT INTO agent_reflections(id, project_id, response_id, trigger,
                                           instruction, deliberation_md, actions_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rid,
                project_id,
                response_id,
                "draft_refine",
                f"refine iter {iter_num}",
                deliberation_md,
                actions_json,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("refine: failed to log reflection (iter %d): %s", iter_num, exc)
