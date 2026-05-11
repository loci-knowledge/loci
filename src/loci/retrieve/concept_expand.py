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
    embedder: Embedder,
    top_k_aspects: int = 5,
) -> list[str]:
    """Return up to `top_k_aspects` aspect labels relevant to `query`.

    Steps:
    1. Extract keywords from the query (2+ char alphanumeric tokens, stop-word
       filtered).
    2. Match keywords against the project's aspect_vocab labels using
       rapidfuzz (cutoff 70). Falls back to a simple substring match if
       rapidfuzz is not installed.
    3. Supplementary: if aspect_embeddings rows exist, run cosine-similarity
       matching for each keyword as an additional signal.
    4. For each matched aspect ID, climb the aspect hierarchy via
       AspectGraphRepository.walk_hierarchy to expand to parent/alias concepts.
    5. For each matched aspect, expand one hop via co_aspect edges in the
       concept graph.
    6. Return up to `top_k_aspects` unique labels, sorted by match score.
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

    # Match keywords → aspect labels (string fuzzy path).
    matched: list[tuple[str, float]] = _match_keywords_to_labels(keywords, labels)

    # Collect matched labels (deduplicated, score-ordered).
    seen: set[str] = set()
    ordered: list[tuple[str, float]] = []
    for label, score in sorted(matched, key=lambda x: -x[1]):
        if label not in seen:
            seen.add(label)
            ordered.append((label, score))

    # Supplementary: embedding-based cosine match (if embeddings are stored).
    embed_results = _embed_match_keywords_to_labels(keywords, conn, project_id)
    for label, score in embed_results:
        if label not in seen:
            seen.add(label)
            ordered.append((label, score))

    if not ordered:
        return []

    # Hierarchy expansion: for each matched aspect, walk parent_of / alias_of.
    try:
        from loci.graph.aspect_graph import AspectGraphRepository  # noqa: PLC0415
        ag_repo = AspectGraphRepository(conn)
        hierarchy_expanded: set[str] = set()
        for label, _score in ordered:
            aspect_id = label_to_id.get(label)
            if aspect_id is None:
                continue
            walked = ag_repo.walk_hierarchy(aspect_id, project_id=project_id)
            for walked_id in walked:
                found = False
                for lbl, aid in label_to_id.items():
                    if aid == walked_id and lbl not in seen:
                        hierarchy_expanded.add(lbl)
                        found = True
                        break
                if not found:
                    row = conn.execute(
                        "SELECT label FROM aspect_vocab WHERE id = ?", (walked_id,)
                    ).fetchone()
                    if row and row["label"] not in seen:
                        hierarchy_expanded.add(row["label"])
    except Exception:  # noqa: BLE001
        hierarchy_expanded = set()

    expanded: set[str] = set(hierarchy_expanded)

    # Expand via co_aspect edges (depth=1) for matched aspects only.
    edge_repo = ConceptEdgeRepository(conn)
    for label, _score in ordered:
        aspect = aspect_repo.get_by_label(label)
        if aspect is None:
            continue
        # Find resources that carry this aspect, then look at their neighbors.
        resource_ids = aspect_repo.resources_for_aspect(label, project_id=project_id, limit=10)
        for rid in resource_ids:
            neighbor_rids = edge_repo.neighbors(rid, edge_types=["co_aspect"], depth=1, project_id=project_id)
            for nrid in neighbor_rids:
                # Collect aspects of the neighbor resource.
                neighbor_aspects = aspect_repo.aspects_for(nrid, project_id=project_id)
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


def aspect_overlap_rank(
    conn: sqlite3.Connection,
    project_id: str,
    expanded_aspects: list[str],
    limit: int = 20,
    query: "Query | None" = None,  # noqa: F821
) -> list[tuple[str, float]]:
    """Rank resources by effective-confidence-weighted aspect overlap.

    Returns list of (resource_id, score) sorted descending.

    v2.2 changes:
    - Reads `aspect_effective_confidence` view instead of raw `pra.confidence`
      so usage events and provenance confirms/rejections steer ranking.
    - When `query` is a structured Query (from split_query), applies typed-field
      multipliers: +0.5 for kind match, +0.5 for role match, +0.3 for target match.
    - Falls back to the legacy `resource_aspects` table when no project-scoped rows exist.

    When expanded_aspects is empty AND query is empty, returns [].
    """
    from loci.retrieve.query_cnl import Query  # noqa: PLC0415

    has_struct_query = query is not None and not query.is_empty

    if not expanded_aspects and not has_struct_query:
        return []

    # --- Structured-query path ---
    if has_struct_query:
        assert query is not None
        params: list = []
        where_clauses: list[str] = ["pra.project_id = ?"]
        params.append(project_id)

        if query.topic is not None:
            if query.topic_fuzzy:
                where_clauses.append("av.topic LIKE ?")
                params.append(f"%{query.topic}%")
            else:
                where_clauses.append("av.topic = ?")
                params.append(query.topic)
        elif expanded_aspects:
            placeholders = ",".join("?" * len(expanded_aspects))
            where_clauses.append(f"av.label IN ({placeholders})")
            params.extend(expanded_aspects)

        if query.kind is not None:
            where_clauses.append("av.kind = ?")
            params.append(query.kind)
        if query.role is not None:
            where_clauses.append("av.role = ?")
            params.append(query.role)
        if query.target is not None:
            target_id_row = conn.execute(
                "SELECT id FROM aspect_vocab WHERE topic = ? AND project_id IS ? LIMIT 1",
                (query.target, project_id),
            ).fetchone() or conn.execute(
                "SELECT id FROM aspect_vocab WHERE topic = ? AND project_id IS NULL LIMIT 1",
                (query.target,),
            ).fetchone()
            if target_id_row:
                where_clauses.append("av.target_aspect_id = ?")
                params.append(target_id_row["id"])

        where_sql = " AND ".join(where_clauses)
        kind_mult = "1.0" if query.kind is None else "(1.0 + 0.5)"
        role_mult = "1.0" if query.role is None else "(1.0 + 0.5)"

        try:
            rows = conn.execute(
                f"""
                SELECT pra.resource_id,
                       SUM(COALESCE(ec.effective_confidence, pra.confidence)
                           * {kind_mult} * {role_mult}) AS score
                FROM project_resource_aspects pra
                JOIN aspect_vocab av ON av.id = pra.aspect_id
                LEFT JOIN aspect_effective_confidence ec
                  ON ec.project_id  = pra.project_id
                 AND ec.resource_id = pra.resource_id
                 AND ec.aspect_id   = pra.aspect_id
                JOIN project_effective_members pm
                  ON pm.node_id = pra.resource_id AND pm.project_id = pra.project_id
                WHERE {where_sql}
                GROUP BY pra.resource_id
                ORDER BY score DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        except Exception:  # noqa: BLE001
            # View may not exist on older schema; fall through to label-match path
            rows = []

        if rows:
            return [(r["resource_id"], float(r["score"])) for r in rows]

    # --- Label-match path (backward compat) ---
    if not expanded_aspects:
        return []

    placeholders = ",".join("?" * len(expanded_aspects))
    try:
        rows = conn.execute(
            f"""
            SELECT pra.resource_id,
                   SUM(COALESCE(ec.effective_confidence, pra.confidence)) AS score
            FROM project_resource_aspects pra
            JOIN aspect_vocab av ON av.id = pra.aspect_id
                                 AND av.label IN ({placeholders})
            LEFT JOIN aspect_effective_confidence ec
              ON ec.project_id  = pra.project_id
             AND ec.resource_id = pra.resource_id
             AND ec.aspect_id   = pra.aspect_id
            JOIN project_effective_members pm
              ON pm.node_id = pra.resource_id AND pm.project_id = pra.project_id
            WHERE pra.project_id = ?
            GROUP BY pra.resource_id
            ORDER BY score DESC
            LIMIT ?
            """,
            (*expanded_aspects, project_id, limit),
        ).fetchall()
    except Exception:  # noqa: BLE001
        # Fall back to raw confidence when the view is absent
        rows = conn.execute(
            f"""
            SELECT pra.resource_id, SUM(pra.confidence) AS score
            FROM project_resource_aspects pra
            JOIN aspect_vocab av ON av.id = pra.aspect_id
                                 AND av.label IN ({placeholders})
            JOIN project_effective_members pm
              ON pm.node_id = pra.resource_id AND pm.project_id = pra.project_id
            WHERE pra.project_id = ?
            GROUP BY pra.resource_id ORDER BY score DESC LIMIT ?
            """,
            (*expanded_aspects, project_id, limit),
        ).fetchall()

    if rows:
        return [(r["resource_id"], float(r["score"])) for r in rows]

    # Fallback: legacy global resource_aspects table.
    rows = conn.execute(
        f"""
        SELECT ra.resource_id, SUM(ra.confidence) AS score
        FROM resource_aspects ra
        JOIN aspect_vocab av ON av.id = ra.aspect_id
                             AND av.label IN ({placeholders})
        JOIN project_effective_members pm
          ON pm.node_id = ra.resource_id
         AND pm.project_id = ?
        GROUP BY ra.resource_id
        ORDER BY score DESC
        LIMIT ?
        """,
        (*expanded_aspects, project_id, limit),
    ).fetchall()

    return [(r["resource_id"], float(r["score"])) for r in rows]


def build_why_surfaced(
    chunk: dict,
    matched_aspects: list[str],
    conn: sqlite3.Connection,
    project_id: str | None = None,
) -> str:
    """Build a human-readable explanation for why a chunk was surfaced.

    E.g.: "matched aspects [methodology, ppr] — source has 3 matching tags"

    `chunk` must have a `resource_id` key.
    `matched_aspects` is the list returned by `expand_query_aspects`.
    `project_id` is optional; when given, project-scoped interpretation is
    prepended to the reason if a ``ProjectInterpretation`` exists for this
    resource.
    """
    resource_id = chunk.get("resource_id", "")
    if not resource_id:
        return "matched by query"

    if not matched_aspects:
        return "matched by keyword/vector search"

    aspect_repo = AspectRepository(conn)
    resource_aspect_rows = aspect_repo.aspects_for(resource_id, project_id=project_id)
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
        base_reason = (
            f"matched aspects [{aspect_str}]"
            + (f" — source has {count} tag(s)" if count else "")
        )
    else:
        base_reason = f"matched by search — aspects: {', '.join(matched_aspects[:3])}"

    # Prepend project-specific interpretation if available.
    if project_id:
        try:
            from loci.graph.interpretations import ProjectInterpretationRepository  # noqa: PLC0415
            interp = ProjectInterpretationRepository(conn).get(project_id, resource_id)
            if interp and interp.summary_md:
                stance = getattr(interp, "stance", "") or ""
                stance_str = f" ({stance})" if stance else ""
                prefix = f"In this project{stance_str}: {interp.summary_md[:120]} — "
                return prefix + base_reason
        except Exception:  # noqa: BLE001
            pass  # interpretations layer not yet available; degrade gracefully

    return base_reason


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _embed_match_keywords_to_labels(
    query_keywords: list[str],
    conn: sqlite3.Connection,
    project_id: str | None,
    top_k: int = 5,
) -> list[tuple[str, float]]:
    """Return *(label, score)* pairs via cosine-similarity on aspect embeddings.

    Supplementary channel: only fires when:
    - AspectEmbedRepository is importable, and
    - There are stored embeddings for this project.

    Each keyword is encoded by the process-global embedder. The resulting
    vectors are matched against stored aspect embeddings. Deduplicated results
    (keeping highest score on collision) are returned.

    Returns [] on any error so the caller can degrade gracefully.
    """
    try:
        from loci.embed.local import get_embedder  # noqa: PLC0415
        from loci.graph.aspect_embed import AspectEmbedRepository  # noqa: PLC0415
    except ImportError:
        return []

    try:
        ae_repo = AspectEmbedRepository(conn)
        embedder = get_embedder()

        aggregated: dict[str, float] = {}
        for keyword in query_keywords:
            if not keyword.strip():
                continue
            query_vec = embedder.encode(keyword)
            hits = ae_repo.cosine_match(query_vec, project_id=project_id, top_k=top_k)
            for label, score in hits:
                if label not in aggregated or score > aggregated[label]:
                    aggregated[label] = score

        return sorted(aggregated.items(), key=lambda x: -x[1])
    except Exception:  # noqa: BLE001
        return []


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
