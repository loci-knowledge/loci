"""Aspect suggestion — synchronous KeyBERT pass + async LLM classification.

Two entry points:

suggest_aspects_sync()
    Fast, CPU-only, synchronous. Called inline during ingest to produce
    immediate suggestions before the result is returned to the caller.
    Uses KeyBERT with the default MiniLM model (a small sentence-transformers
    model that loads lazily). Matches keywords against existing_vocab via
    rapidfuzz; falls back to raw keywords when the vocab is empty.

classify_aspects_llm()
    Async, LLM-backed, used by the background classify_aspects job. Builds
    a prompt with title + abstract and asks the configured RAG model to
    classify into aspects, returning (label, confidence) pairs.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loci.config import Settings

log = logging.getLogger(__name__)

_KEYBERT_INSTANCE = None
_KEYBERT_LOCK = None


def _get_keybert():
    """Lazily initialise a shared KeyBERT instance (thread-safe)."""
    global _KEYBERT_INSTANCE, _KEYBERT_LOCK
    import threading

    if _KEYBERT_LOCK is None:
        _KEYBERT_LOCK = threading.Lock()

    if _KEYBERT_INSTANCE is not None:
        return _KEYBERT_INSTANCE

    with _KEYBERT_LOCK:
        if _KEYBERT_INSTANCE is None:
            try:
                from keybert import KeyBERT  # type: ignore[import-not-found]
                _KEYBERT_INSTANCE = KeyBERT()
                log.debug("KeyBERT loaded successfully")
            except ImportError:
                log.warning("keybert not installed; aspect suggestions will be empty")
                _KEYBERT_INSTANCE = None

    return _KEYBERT_INSTANCE


def suggest_aspects_sync(
    text: str,
    existing_vocab: list[str],
    top_k: int = 5,
) -> list[str]:
    """Run KeyBERT on text[:3000], match against existing_vocab.

    Returns up to top_k aspect labels. Existing vocab labels are preferred;
    raw keyword phrases are returned as fallback when the vocab is empty or
    no match exceeds the cutoff.
    """
    kb = _get_keybert()
    if kb is None:
        return []

    snippet = text[:3000]
    if not snippet.strip():
        return []

    try:
        # Extract top 10 keyphrases (1- and 2-gram). KeyBERT returns
        # [(keyword, score), ...] sorted by relevance descending.
        raw_keywords: list[tuple[str, float]] = kb.extract_keywords(
            snippet,
            keyphrase_ngram_range=(1, 2),
            top_n=10,
            stop_words="english",
        )
    except Exception:  # noqa: BLE001
        log.exception("KeyBERT extraction failed")
        return []

    keyword_strs = [kw for kw, _score in raw_keywords]

    if not existing_vocab:
        # No vocab yet — return raw keywords as candidate labels.
        return keyword_strs[:top_k]

    # Match each keyword against the existing vocab using rapidfuzz.
    try:
        from rapidfuzz import fuzz as rf_fuzz
        from rapidfuzz import process as rf_process
    except ImportError:
        log.warning("rapidfuzz not installed; returning raw keywords")
        return keyword_strs[:top_k]

    matched: set[str] = set()
    for kw in keyword_strs:
        results = rf_process.extractBests(
            kw,
            existing_vocab,
            scorer=rf_fuzz.token_set_ratio,
            score_cutoff=60,
            limit=2,
        )
        for label, _score, _idx in results:
            matched.add(label)
            if len(matched) >= top_k:
                break
        if len(matched) >= top_k:
            break

    # If matched vocab labels are fewer than top_k, pad with raw keywords not
    # already captured.
    result = list(matched)
    for kw in keyword_strs:
        if len(result) >= top_k:
            break
        if kw not in result:
            result.append(kw)

    return result[:top_k]


async def classify_aspects_llm(
    text: str,
    title: str,
    existing_vocab: list[str],
    settings: Settings,
) -> list[tuple[str, float]]:
    """LLM-backed aspect classification. Returns (label, confidence) pairs.

    Calls the configured RAG model with a structured prompt asking for JSON
    output: {"aspects": [{"label": "...", "confidence": 0.9}, ...]}.
    Falls back gracefully (returns []) if the LLM is not configured or the
    response cannot be parsed.
    """
    from loci.llm.agent import LLMNotConfiguredError, build_agent

    abstract = text[:1000]
    vocab_list = ", ".join(existing_vocab[:50]) if existing_vocab else "(none yet)"

    instructions = (
        "You are an expert scientific librarian. "
        "Given a document title and abstract, classify it into 3-7 aspect labels. "
        "Prefer labels from the existing vocabulary when they fit. "
        "You may propose new labels if needed. "
        "Respond with valid JSON only, in this exact format:\n"
        '{"aspects": [{"label": "...", "confidence": 0.9}, ...]}'
    )
    user_msg = (
        f"Title: {title}\n\n"
        f"Abstract:\n{abstract}\n\n"
        f"Existing vocabulary: {vocab_list}\n\n"
        "Classify this document into aspects."
    )

    try:
        agent = build_agent(
            settings.rag_model,
            instructions=instructions,
            output_type=str,
            settings=settings,
        )
    except LLMNotConfiguredError:
        log.info("classify_aspects_llm: LLM not configured; skipping")
        return []

    try:
        result = await agent.run(user_msg)
        raw_text = result.output if hasattr(result, "output") else str(result)
    except Exception:  # noqa: BLE001
        log.exception("classify_aspects_llm: agent.run failed")
        return []

    # Parse JSON response
    try:
        # Strip markdown fences if present
        clean = raw_text.strip()
        if clean.startswith("```"):
            lines = clean.splitlines()
            clean = "\n".join(
                line for line in lines
                if not line.startswith("```")
            ).strip()
        parsed = json.loads(clean)
        items = parsed.get("aspects", [])
        pairs: list[tuple[str, float]] = []
        for item in items:
            if isinstance(item, dict) and "label" in item:
                label = str(item["label"]).strip()
                confidence = float(item.get("confidence", 0.8))
                if label:
                    pairs.append((label, confidence))
        return pairs
    except (json.JSONDecodeError, KeyError, ValueError):
        log.warning(
            "classify_aspects_llm: could not parse LLM response: %r",
            raw_text[:200],
        )
        return []
