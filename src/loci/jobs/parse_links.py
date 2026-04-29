"""parse_links job — extract wikilinks + citations from a resource and write
concept_edges.

Triggered after ingest for markdown, notes, and similar text resources.

Payload shape:
    {
      "resource_id": "<ULID>",   # required
      "project_id":  "<ULID>"    # required
    }
"""

from __future__ import annotations

import logging
import sqlite3

from loci.graph.concept_edges import ConceptEdgeRepository

log = logging.getLogger(__name__)


async def handle_parse_links(job: dict, conn: sqlite3.Connection, settings) -> dict:
    """Extract wikilinks, citations, and co-aspect edges for a resource.

    Steps:
    1. Load resource text and subkind.
    2. Call parse_links(text, subkind) → ParsedLinks.
    3. Wikilinks: call resolve_wikilinks(wikilinks, project_id, conn)
       → write concept_edges with edge_type="wikilink" for each resolved pair.
    4. Citation keys: fuzzy-match @key against raw_nodes titles in the project
       → write concept_edges with edge_type="cites" for each match.
    5. Co-aspect edges: for resources that share 2+ aspect labels with the
       current resource, write concept_edges with edge_type="co_aspect".
       Only considers existing resources already in the project.
    """
    payload = job.get("payload", {})
    resource_id = payload.get("resource_id")
    project_id = payload.get("project_id")

    if not resource_id:
        raise ValueError("parse_links: payload missing resource_id")
    if not project_id:
        raise ValueError("parse_links: payload missing project_id")

    # 1. Load resource text and subkind.
    row = conn.execute(
        """
        SELECT n.body, n.subkind
        FROM nodes n
        JOIN raw_nodes r ON r.node_id = n.id
        WHERE n.id = ? AND n.kind = 'raw'
        """,
        (resource_id,),
    ).fetchone()

    if row is None:
        log.warning("parse_links: resource not found: %s", resource_id)
        return {"skipped": True, "reason": "resource not found"}

    text = row["body"] or ""
    subkind = row["subkind"] or "txt"

    # 2. Parse links from the text.
    from loci.capture.link_parser import parse_links, resolve_wikilinks

    parsed = parse_links(text, subkind)

    edges_repo = ConceptEdgeRepository(conn)
    wikilink_count = 0
    cites_count = 0
    co_aspect_count = 0

    # 3. Wikilinks → concept_edges with edge_type="wikilink".
    if parsed.wikilinks:
        resolved = resolve_wikilinks(parsed.wikilinks, project_id, conn)
        for _target, dst_id in resolved:
            if dst_id == resource_id:
                continue  # no self-edges
            edges_repo.add_edge(
                src_id=resource_id,
                dst_id=dst_id,
                edge_type="wikilink",
                weight=1.0,
            )
            wikilink_count += 1

    # 4. Citation keys → concept_edges with edge_type="cites".
    if parsed.citation_keys:
        cites_count = _resolve_citations(
            conn, edges_repo, resource_id, project_id, parsed.citation_keys
        )

    # 5. Co-aspect edges: resources sharing 2+ aspects.
    co_aspect_count = _write_co_aspect_edges(
        conn, edges_repo, resource_id, project_id
    )

    log.info(
        "parse_links: resource=%s wikilinks=%d cites=%d co_aspect=%d",
        resource_id, wikilink_count, cites_count, co_aspect_count,
    )
    return {
        "skipped": False,
        "wikilink_edges": wikilink_count,
        "cites_edges": cites_count,
        "co_aspect_edges": co_aspect_count,
    }


def _resolve_citations(
    conn: sqlite3.Connection,
    edges_repo: ConceptEdgeRepository,
    resource_id: str,
    project_id: str,
    citation_keys: list[str],
) -> int:
    """Fuzzy-match citation @keys against raw_nodes titles in the project.

    Queries raw_nodes titles and uses rapidfuzz token_set_ratio with a cutoff
    of 75 to find matches. Writes a concept_edge(edge_type="cites") per hit.
    Returns the number of edges written.
    """
    rows = conn.execute(
        """
        SELECT n.id, n.title
        FROM nodes n
        JOIN raw_nodes r ON r.node_id = n.id
        JOIN project_effective_members pm ON pm.node_id = n.id
        WHERE pm.project_id = ? AND n.kind = 'raw' AND n.id != ?
        """,
        (project_id, resource_id),
    ).fetchall()

    if not rows:
        return 0

    try:
        from rapidfuzz import fuzz as rf_fuzz
        from rapidfuzz import process as rf_process
        _has_rapidfuzz = True
    except ImportError:
        log.warning("parse_links._resolve_citations: rapidfuzz not installed; skipping")
        return 0

    all_titles = [(r["id"], r["title"] or "") for r in rows if r["title"]]
    title_strings = [t for _id, t in all_titles]
    id_by_title: dict[str, str] = {t: rid for rid, t in all_titles}

    count = 0
    for key in citation_keys:
        # Citation keys look like "author2023title" — match against titles.
        best = rf_process.extractOne(
            key,
            title_strings,
            scorer=rf_fuzz.token_set_ratio,
            score_cutoff=75,
        )
        if best is not None:
            matched_title, _score, _idx = best
            dst_id = id_by_title.get(matched_title)
            if dst_id and dst_id != resource_id:
                edges_repo.add_edge(
                    src_id=resource_id,
                    dst_id=dst_id,
                    edge_type="cites",
                    metadata={"citation_key": key},
                    weight=1.0,
                )
                count += 1

    return count


def _write_co_aspect_edges(
    conn: sqlite3.Connection,
    edges_repo: ConceptEdgeRepository,
    resource_id: str,
    project_id: str,
) -> int:
    """Write co_aspect edges to all other resources sharing 2+ aspect labels.

    Queries resource_aspects for the current resource, then finds other resources
    in the project that share at least 2 of those aspect_ids. Writes a
    concept_edge(edge_type="co_aspect") for each such pair (both directions are
    NOT written — we write only src=resource_id → dst=other to avoid symmetric
    duplicates; `add_edge` is idempotent so re-running is safe).

    Returns the number of edges written.
    """
    # Get the current resource's aspect_ids.
    my_aspects = conn.execute(
        "SELECT aspect_id FROM resource_aspects WHERE resource_id = ?",
        (resource_id,),
    ).fetchall()
    if len(my_aspects) < 2:
        # Need at least 2 aspects for a co_aspect edge to make sense.
        return 0

    my_aspect_ids = [r["aspect_id"] for r in my_aspects]
    placeholders = ",".join("?" * len(my_aspect_ids))

    # Find other resources in the project that share 2+ of these aspects.
    other_resources = conn.execute(
        f"""
        SELECT ra.resource_id, COUNT(ra.aspect_id) AS shared
        FROM resource_aspects ra
        JOIN project_effective_members pm ON pm.node_id = ra.resource_id
        WHERE pm.project_id = ?
          AND ra.resource_id != ?
          AND ra.aspect_id IN ({placeholders})
        GROUP BY ra.resource_id
        HAVING shared >= 2
        """,
        (project_id, resource_id, *my_aspect_ids),
    ).fetchall()

    count = 0
    for row in other_resources:
        dst_id = row["resource_id"]
        shared = row["shared"]
        edges_repo.add_edge(
            src_id=resource_id,
            dst_id=dst_id,
            edge_type="co_aspect",
            weight=min(1.0, shared / max(len(my_aspect_ids), 1)),
            metadata={"shared_aspect_count": shared},
        )
        count += 1

    return count
