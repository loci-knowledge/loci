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


def _strip_markdown_fences(text: str) -> str:
    """Strip leading/trailing markdown code fences from LLM output."""
    clean = text.strip()
    if clean.startswith("```"):
        clean = "\n".join(
            line for line in clean.splitlines()
            if not line.startswith("```")
        ).strip()
    return clean


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
        "You are an expert knowledge librarian. "
        "Given a document title and abstract, classify it into 3-7 aspect labels. "
        "Labels MUST be in English, short (1-4 words), and use noun phrases. "
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

    try:
        parsed = json.loads(_strip_markdown_fences(raw_text))
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


async def classify_project_interpretation_llm(
    text: str,
    title: str,
    existing_vocab: list[str],
    project_profile_md: str,
    recent_queries: list[str],
    gold_labels: list[str],
    prior_summary: str | None,
    settings: "Settings",
    conversation_snippets: list[str] | None = None,
) -> "InterpretationOutput":
    """LLM-backed project-scoped interpretation of a document.

    Returns an ``InterpretationOutput`` with project-specific aspects, a
    1-2 sentence summary, a stance label, and optional typed relations.
    Falls back to an empty ``InterpretationOutput`` on any error.
    """
    from loci.graph.models import InterpretationOutput  # noqa: PLC0415 — lazy to avoid circular
    from loci.llm.agent import LLMNotConfiguredError, build_agent  # noqa: PLC0415

    abstract = text[:1000]
    vocab_list = ", ".join(existing_vocab[:50]) if existing_vocab else "(none yet)"
    queries_str = "\n".join(f"- {q}" for q in recent_queries[-10:]) if recent_queries else "(none)"
    gold_str = ", ".join(gold_labels) if gold_labels else "(none)"
    prior_str = f"\nPrior summary: {prior_summary}" if prior_summary else ""
    conv_str = ""
    if conversation_snippets:
        snippets = conversation_snippets[-5:]
        conv_str = "\nRecent conversation context:\n" + "\n".join(f"- {s[:200]}" for s in snippets)

    instructions = (
        "You are a research librarian. Interpret this document through the lens of a specific "
        "research project. Produce a project-specific reading: which aspects apply in this "
        "project's context, a 1-2 sentence summary of what this document means for the "
        "project's goals, the document's stance relative to the project (one of: methodological, "
        "supporting, contradictory, reference, tangential), and typed semantic relations to other "
        "resources if any patterns are evident. Respond with valid JSON only.\n\n"
        "Each `proposition` is a controlled-English phrase: `topic [as kind] [role target] [key=value ...]`.\n"
        "Kinds (optional): methodology, technique, concept, tool, resource, critique, reference, claim, dataset.\n"
        "Roles (optional, closed set): exemplifies, critiques, supports, extends, reviews, rejects, "
        "applies, introduces, rebuts, grounds.\n"
        "If unsure of kind or role, emit only the topic slug (e.g. \"reproducibility\")."
    )
    user_msg = (
        f"Title: {title}\n\n"
        f"Abstract:\n{abstract}\n\n"
        f"Project profile:\n{project_profile_md}\n\n"
        f"Recent search queries from this project:\n{queries_str}\n\n"
        f"Existing vocabulary: {vocab_list}\n\n"
        f"Gold labels (user-set, do not contradict): {gold_str}"
        f"{prior_str}{conv_str}\n\n"
        "Return JSON in this exact format:\n"
        "{\n"
        '  "aspects": [{"proposition": "reproducibility as critique critiques frequentist-statistics", '
        '"confidence": 0.9, "rationale": "..."}],\n'
        '  "summary_md": "...",\n'
        '  "stance": "supporting",\n'
        '  "relations": [{"target_resource_id": "...", "edge_type": "supports", "weight": 0.8, "evidence": "..."}]\n'
        "}"
    )

    try:
        agent = build_agent(
            settings.rag_model,
            instructions=instructions,
            output_type=str,
            settings=settings,
        )
    except LLMNotConfiguredError:
        log.info("classify_project_interpretation_llm: LLM not configured; skipping")
        return InterpretationOutput()

    try:
        result = await agent.run(user_msg)
        raw_text = result.output if hasattr(result, "output") else str(result)
    except Exception:  # noqa: BLE001
        log.exception("classify_project_interpretation_llm: agent.run failed")
        return InterpretationOutput()

    try:
        parsed = json.loads(_strip_markdown_fences(raw_text))

        from loci.graph.models import AspectScore, Relation  # noqa: PLC0415

        from loci.graph.aspect_dsl import parse as parse_proposition  # noqa: PLC0415
        from loci.graph.aspect_dsl import render as render_proposition  # noqa: PLC0415

        aspects: list[AspectScore] = []
        for item in parsed.get("aspects", []):
            if not isinstance(item, dict):
                continue
            # Accept both new `proposition` key and legacy `label` key
            raw_prop = str(item.get("proposition") or item.get("label", "")).strip()
            if not raw_prop:
                continue
            confidence = float(item.get("confidence", 0.8))
            rationale = str(item.get("rationale", "")).strip()
            prop = parse_proposition(raw_prop)
            # label is always the rendered canonical form so writers stay compatible
            label = render_proposition(prop) if not prop.is_flat else prop.topic
            aspects.append(
                AspectScore(label=label, confidence=confidence, rationale=rationale or None,
                            proposition=prop)
            )

        summary_md = str(parsed.get("summary_md", "")).strip()
        stance = str(parsed.get("stance", "")).strip()

        relations: list[Relation] = []
        for rel in parsed.get("relations", []):
            if isinstance(rel, dict) and "target_resource_id" in rel and "edge_type" in rel:
                relations.append(Relation(
                    target_resource_id=str(rel["target_resource_id"]).strip(),
                    edge_type=str(rel["edge_type"]).strip(),
                    weight=float(rel.get("weight", 0.8)),
                    evidence=str(rel.get("evidence", "")).strip(),
                ))

        return InterpretationOutput(
            aspects=aspects,
            summary_md=summary_md,
            stance=stance,
            relations=relations,
        )
    except (json.JSONDecodeError, KeyError, ValueError):
        log.warning(
            "classify_project_interpretation_llm: could not parse LLM response: %r",
            raw_text[:200],
        )
        return InterpretationOutput()
