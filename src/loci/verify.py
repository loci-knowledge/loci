"""Claim-level entailment verifier for drafted text.

Why this exists: the draft engine enforces that every [Cn] handle in the
output maps to a real candidate raw — that prevents citing-into-the-void.
What it does NOT prevent is the most common modern hallucination: a
plausibly-worded sentence followed by a real handle that points at a chunk
which does not actually support the sentence. KG2RAG and other graph-RAG
systems rely on chunk-level grounding to make this cheap to check.

This module runs a single LLM call after generation. For each (claim,
[Cn]) pair we ask: does the chunk text behind Cn entail the claim? The
verdict is one of {supported, partial, unsupported}; the answer comes back
as a Pydantic model so we can attach verdicts to citations and propagate
them to the UI.

The verifier is best-effort. If the LLM is not configured, or the call
fails, citations are returned with verdict=unknown and the draft is
unchanged — we never block a draft on verification.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from loci.config import get_settings
from loci.llm import LLMNotConfiguredError, build_agent

log = logging.getLogger(__name__)

Verdict = Literal["supported", "partial", "unsupported", "unknown"]

# Regex for splitting output text into "claims": sentences that contain at
# least one [Cn] marker. We split on sentence boundaries (period / question /
# exclamation followed by whitespace) but keep markers attached to the
# sentence they end.
_SENTENCE_RE = re.compile(r"[^.!?]*[.!?]+(?=\s|$)|[^.!?]+$", re.DOTALL)
_CITE_RE = re.compile(r"\[C(\d+)\]", re.IGNORECASE)


@dataclass
class ClaimUnit:
    """One sentence + the citation handle(s) it carries."""
    text: str          # the sentence text
    handles: list[str] # ["C1", "C7", ...]


class _VerdictItem(BaseModel):
    """LLM output schema — one row per (sentence, handle) we asked about."""
    sentence_index: int = Field(description="0-based index into the input sentence list")
    handle: str = Field(description="Citation handle being judged (e.g. 'C3')")
    verdict: Literal["supported", "partial", "unsupported"] = Field(
        description="supported = chunk directly entails the claim. "
                    "partial = chunk is on-topic and adjacent but doesn't "
                    "fully entail the specific claim. "
                    "unsupported = chunk doesn't say this.",
    )
    reason: str = Field(description="One terse sentence justifying the verdict.")


class _VerifierOutput(BaseModel):
    items: list[_VerdictItem]


@dataclass
class CitationVerdict:
    handle: str          # "C3"
    sentence_index: int  # which sentence the handle appears in
    verdict: Verdict
    reason: str


@dataclass
class VerifyResult:
    verdicts: list[CitationVerdict]
    # If anything went wrong (LLM unconfigured / call failed), this is set.
    # The verdicts list will be empty in that case.
    error: str | None = None


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def split_claims(output_md: str) -> list[ClaimUnit]:
    """Split a drafted markdown into (sentence, handles) units.

    Sentences without any [Cn] markers are dropped — they don't need
    verification. Markers are normalized to upper-case "C<digits>".
    """
    units: list[ClaimUnit] = []
    for m in _SENTENCE_RE.finditer(output_md):
        sent = m.group(0).strip()
        if not sent:
            continue
        handles = [f"C{int(h.group(1))}" for h in _CITE_RE.finditer(sent)]
        if not handles:
            continue
        units.append(ClaimUnit(text=sent, handles=handles))
    return units


def verify(
    output_md: str,
    chunks_by_handle: dict[str, str],
) -> VerifyResult:
    """Score each (sentence, [Cn]) pair against the chunk text behind Cn.

    `chunks_by_handle` maps "C1" → the chunk text the citation should
    support. If a handle is missing from the dict we mark the verdict as
    unknown (the candidate raw was selected, but no chunk-level span was
    available — happens for raws retrieved via routing-only).

    Returns VerifyResult.verdicts in the order they appear in the draft.
    Empty + error-set if the LLM call could not run.
    """
    claims = split_claims(output_md)
    if not claims:
        return VerifyResult(verdicts=[])

    # Build the (sentence, handle) work list, filtering out unknown handles
    # so the LLM only judges what it can. Track the unknowns so we still
    # return a verdict for them.
    work: list[tuple[int, str, str, str]] = []  # (idx, sent, handle, chunk)
    unknown: list[CitationVerdict] = []
    for i, claim in enumerate(claims):
        for handle in claim.handles:
            chunk_text = chunks_by_handle.get(handle)
            if chunk_text:
                work.append((i, claim.text, handle, chunk_text))
            else:
                unknown.append(CitationVerdict(
                    handle=handle, sentence_index=i,
                    verdict="unknown",
                    reason="no chunk-level span available for this citation",
                ))

    if not work:
        return VerifyResult(verdicts=unknown)

    def _all_unknown(reason: str) -> list[CitationVerdict]:
        """Return one unknown verdict per (sentence, handle) pair we asked
        about, plus the pre-collected unknowns from missing chunks. Used
        when the LLM is unavailable so the caller still sees every handle."""
        out = list(unknown)
        for idx, _sent, handle, _chunk in work:
            out.append(CitationVerdict(
                handle=handle, sentence_index=idx,
                verdict="unknown", reason=reason,
            ))
        out.sort(key=lambda v: (v.sentence_index, v.handle))
        return out

    settings = get_settings()
    try:
        agent = build_agent(
            settings.rag_model,
            instructions=_SYSTEM_PROMPT,
            output_type=_VerifierOutput,
        )
    except LLMNotConfiguredError as exc:
        log.warning("verify: %s; skipping verification", exc)
        return VerifyResult(
            verdicts=_all_unknown("verifier LLM not configured"),
            error=str(exc),
        )

    user_msg = _build_prompt(work)
    try:
        result = agent.run_sync(user_msg)
    except Exception as exc:  # noqa: BLE001
        log.warning("verify: LLM call failed: %s", exc)
        return VerifyResult(
            verdicts=_all_unknown(f"verifier call failed: {exc}"),
            error=str(exc),
        )

    out = result.output
    if not isinstance(out, _VerifierOutput):
        return VerifyResult(verdicts=unknown, error="verifier returned non-structured output")

    # Build a lookup from the LLM's verdicts; preserve original input order.
    by_key: dict[tuple[int, str], _VerdictItem] = {
        (v.sentence_index, v.handle.upper()): v for v in out.items
    }
    verdicts: list[CitationVerdict] = list(unknown)
    for idx, _sent, handle, _chunk in work:
        item = by_key.get((idx, handle))
        if item is None:
            verdicts.append(CitationVerdict(
                handle=handle, sentence_index=idx,
                verdict="unknown", reason="verifier did not score this pair",
            ))
        else:
            verdicts.append(CitationVerdict(
                handle=handle, sentence_index=idx,
                verdict=item.verdict, reason=item.reason,
            ))
    # Sort by (sentence_index, handle) for deterministic display.
    verdicts.sort(key=lambda v: (v.sentence_index, v.handle))
    return VerifyResult(verdicts=verdicts)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = """\
You are loci's citation verifier. For each (claim, citation) pair the user
gives you, judge whether the cited chunk *entails* the claim.

Definitions:
- supported   : the chunk directly states (or unambiguously implies) the claim.
- partial     : the chunk is on-topic and adjacent, but does not fully entail
                the specific claim. (e.g. the chunk mentions the same concept
                but not the specific number / property the claim asserts.)
- unsupported : the chunk does not say this. The claim may still be true
                in the world, but it cannot be drawn from this chunk.

Rules:
- Be strict. "Adjacent and plausible" is `partial`, not `supported`.
- A claim that requires synthesizing across multiple sentences in the chunk
  is `supported` only if the chunk really contains all the pieces.
- Hedged claims ("may help", "is sometimes used") are easier to support
  than absolute claims ("X is the only Y"). Calibrate accordingly.
- Reasons: one terse sentence. No fluff. Quote a few words from the chunk
  if it sharpens the verdict.

Return one item per (sentence, handle) pair you were asked about.
"""


def _build_prompt(work: list[tuple[int, str, str, str]]) -> str:
    parts = ["Score each pair below. Return one verdict per row.\n"]
    for idx, sent, handle, chunk in work:
        parts.append(
            f"--- pair index={idx} handle={handle} ---\n"
            f"CLAIM: {sent}\n\n"
            f"CHUNK ({handle}):\n{chunk[:2400]}\n",
        )
    return "\n".join(parts)
