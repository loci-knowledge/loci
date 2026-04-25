"""HyDE — Hypothetical Document Embeddings.

Reference: Gao et al., "Precise Zero-Shot Dense Retrieval without Relevance
Labels" (arXiv 2212.10496). Implemented inline (no LangChain) so it shares
our embedder and never picks a different normaliser.

The recipe is dead simple:

    answer = LLM(query, "write a 1-paragraph hypothetical answer")
    vec    = embed(answer)
    hits   = ann(vec)

We use `Settings.hyde_model` for the LLM call — typically a fast/cheap model
since the output is throwaway. If the configured provider has no API key,
we silently fall back to the original query (the embedder still works).
"""

from __future__ import annotations

import logging

from loci.config import get_settings
from loci.llm import LLMNotConfiguredError, build_agent

log = logging.getLogger(__name__)


_HYDE_INSTRUCTIONS = (
    "You are helping a retrieval system. Given a user's query, write a single "
    "concise paragraph (<= 120 words) that *could plausibly be a passage from "
    "a document* that answers the query directly. Do not hedge, do not refuse, "
    "do not say you don't know — even if you have to fabricate plausible "
    "details, the goal is to produce a passage shaped like a real answer. The "
    "passage will be embedded and used to find similar real passages. Output "
    "only the paragraph; no preamble, no metadata."
)


def hypothesize(query: str) -> str:
    """Return a hypothetical passage for `query`, or the query verbatim if no LLM.

    The fallback is intentional: callers can always pass the result to the
    embedder. They don't need to special-case "no LLM".
    """
    settings = get_settings()
    try:
        agent = build_agent(settings.hyde_model, instructions=_HYDE_INSTRUCTIONS)
    except LLMNotConfiguredError as exc:
        log.debug("HyDE: %s; returning query verbatim", exc)
        return query
    try:
        result = agent.run_sync(query)
    except Exception as exc:  # noqa: BLE001 — never let HyDE break retrieval
        log.warning("HyDE call failed; falling back to query: %s", exc)
        return query
    text = (result.output or "").strip()
    return text or query
