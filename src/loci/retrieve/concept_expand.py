"""Concept-graph query expansion.

Given a user query, expands it into a set of aspect labels drawn from the
aspect vocabulary and the concept graph (co_aspect edges). These labels are
used as retrieval filters/boosters by the pipeline.

Two public functions:

- `expand_query_aspects` — returns a list of aspect labels relevant to the query.
- `build_why_surfaced`   — returns a human-readable explanation for why a chunk
                           was included in the results.
"""

from __future__ import annotations

import re
import sqlite3

from loci.embed.local import Embedder
from loci.graph.aspects import AspectRepository
from loci.graph.concept_edges import ConceptEdgeRepository

# Minimum token length to consider as a keyword.
_MIN_TOKEN_LEN = 2

# Minimum fuzzy-match score to accept an aspect label match.
_FUZZY_CUTOFF = 70

_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "in", "on", "at", "to", "of",
    "for", "with", "is", "are", "was", "be", "by", "that", "this",
    "it", "as", "from", "but", "not", "have", "has", "do", "does",
    "did", "will", "would", "could", "should", "can", "about",
    "which", "what", "how", "when", "where", "who", "why",
})

_NON_ALPHANUM = re.compile(r"[^a-zA-Z0-9 ]")


def expand_query_aspects(
    query: str,
    project_id: str,
    conn: sqlite3.Connection,
    embedder: Embedder,  # accepted but unused in the current keyword-match path
    top_k_aspects: int = 5,
) -> list[str]:
    """Return up to `top_k_aspects` aspect labels relevant to `query`.

    Steps:
    1. Extract keywords from the query (2+ char alphanumeric tokens, stop-word
       filtered).
    2. Match keywords against the project's aspect_vocab labels using
       rapidfuzz (cutoff 70). Falls back to a simple substring match if
       rapidfuzz is not installed.
    3. For each matched aspect, expand one hop via co_aspect edges in the
       concept graph.
    4. Return up to `top_k_aspects` unique labels, sorted by match score.
    """
    keywords = _extract_keywords(query)
    if not keywords:
        return []

    # Load all aspect labels for this project so we have a local vocabulary.
    aspect_repo = AspectRepository(conn)
    all_aspects = aspect_repo.list_vocab(project_id=project_id)
    if not all_aspects:
        return []

    labels = [a.label for a in all_aspects]
    label_to_id = {a.label: a.id for a in all_aspects}

    # Match keywords → aspect labels.
    matched: list[tuple[str, float]] = _match_keywords_to_labels(keywords, labels)

    # Collect matched labels (deduplicated, score-ordered).
    seen: set[str] = set()
    ordered: list[tuple[str, float]] = []
    for label, score in sorted(matched, key=lambda x: -x[1]):
        if label not in seen:
            seen.add(label)
            ordered.append((label, score))

    if not ordered:
        return []

    # Expand via co_aspect edges (depth=1) for matched aspects only.
    edge_repo = ConceptEdgeRepository(conn)
    expanded: set[str] = set()
    for label, _score in ordered:
        aspect = aspect_repo.get_by_label(label)
        if aspect is None:
            continue
        # Find resources that carry this aspect, then look at their neighbors.
        resource_ids = aspect_repo.resources_for_aspect(label, project_id=project_id, limit=10)
        for rid in resource_ids:
            neighbor_rids = edge_repo.neighbors(rid, edge_types=["co_aspect"], depth=1)
            for nrid in neighbor_rids:
                # Collect aspects of the neighbor resource.
                neighbor_aspects = aspect_repo.aspects_for(nrid)
                for ra in neighbor_aspects:
                    neighbor_label = _aspect_id_to_label(ra.aspect_id, label_to_id)
                    if neighbor_label and neighbor_label not in seen:
                        expanded.add(neighbor_label)

    # Build the final list: direct matches first, then graph-expanded ones.
    result: list[str] = [label for label, _score in ordered]
    for label in sorted(expanded):
        if label not in seen:
            result.append(label)
            seen.add(label)

    return result[:top_k_aspects]


def build_why_surfaced(
    chunk: dict,
    matched_aspects: list[str],
    conn: sqlite3.Connection,
) -> str:
    """Build a human-readable explanation for why a chunk was surfaced.

    E.g.: "matched aspects [methodology, ppr] — source has 3 matching tags"

    `chunk` must have a `resource_id` key.
    `matched_aspects` is the list returned by `expand_query_aspects`.
    """
    resource_id = chunk.get("resource_id", "")
    if not resource_id:
        return "matched by query"

    if not matched_aspects:
        return "matched by keyword/vector search"

    aspect_repo = AspectRepository(conn)
    resource_aspect_rows = aspect_repo.aspects_for(resource_id)
    resource_labels = {
        _lookup_aspect_label(ra.aspect_id, conn)
        for ra in resource_aspect_rows
    }
    # Remove None values.
    resource_labels.discard(None)  # type: ignore[arg-type]

    overlap = [a for a in matched_aspects if a in resource_labels]

    if overlap:
        aspect_str = ", ".join(overlap[:3])
        count = len(resource_labels)
        return (
            f"matched aspects [{aspect_str}]"
            + (f" — source has {count} tag(s)" if count else "")
        )

    return f"matched by search — aspects: {', '.join(matched_aspects[:3])}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_keywords(query: str) -> list[str]:
    """Return unique lowercase alphanumeric tokens from `query`, stop-word filtered."""
    cleaned = _NON_ALPHANUM.sub(" ", query.lower())
    seen: set[str] = set()
    out: list[str] = []
    for token in cleaned.split():
        if len(token) >= _MIN_TOKEN_LEN and token not in _STOP_WORDS and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def _match_keywords_to_labels(
    keywords: list[str],
    labels: list[str],
) -> list[tuple[str, float]]:
    """Return (label, score) pairs where any keyword fuzzy-matches the label.

    Tries rapidfuzz first (preferred); falls back to case-insensitive
    substring containment if rapidfuzz is unavailable.
    """
    try:
        from rapidfuzz import fuzz
        from rapidfuzz import process as rfprocess

        results: list[tuple[str, float]] = []
        for keyword in keywords:
            hits = rfprocess.extract(
                keyword,
                labels,
                scorer=fuzz.partial_ratio,
                limit=5,
                score_cutoff=_FUZZY_CUTOFF,
            )
            for label, score, _idx in hits:
                results.append((label, float(score)))
        return results

    except ImportError:
        # Simple fallback: substring containment treated as score 75.
        results = []
        kw_set = set(keywords)
        for label in labels:
            label_lower = label.lower()
            for kw in kw_set:
                if kw in label_lower or label_lower in kw:
                    results.append((label, 75.0))
                    break
        return results


def _aspect_id_to_label(
    aspect_id: str,
    label_to_id: dict[str, str],
) -> str | None:
    """Reverse-lookup an aspect label from the pre-built label→id map."""
    for label, aid in label_to_id.items():
        if aid == aspect_id:
            return label
    return None


def _lookup_aspect_label(aspect_id: str, conn: sqlite3.Connection) -> str | None:
    """Look up an aspect label by id directly from the DB."""
    row = conn.execute(
        "SELECT label FROM aspect_vocab WHERE id = ?", (aspect_id,)
    ).fetchone()
    return row["label"] if row else None
