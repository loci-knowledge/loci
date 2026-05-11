"""infer_interpretation job — LLM-backed per-project interpretation of a resource.

Triggered by: recall access, save ingest, usage threshold, conversation events,
or a periodic sweep. Generates a project-scoped summary, stance, aspect scores,
and typed resource relations.

Payload shape:
    {
      "resource_id": "<ULID>",
      "project_id":  "<ULID>",
      "trigger":     "recall|save|usage|conversation|sweep"
    }

`project_id` is also available on `job["project_id"]` (set at enqueue time).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

log = logging.getLogger(__name__)


async def handle_infer_interpretation(
    job: dict, conn: sqlite3.Connection, settings
) -> dict:
    """Generate a project-scoped interpretation of a resource.

    Steps:
    1.  Load resource text (nodes + raw_nodes).
    2.  Load project profile.
    3.  Get top-10 aspects for the project.
    4.  Get last-10 recall queries for the project.
    5.  Compute inputs_hash; skip if unchanged.
    6.  Get user-gold aspect labels (never overwritten).
    7.  Get recent conversation events.
    8.  Call classify_project_interpretation_llm().
    9.  Write project_resource_aspects (skip gold labels).
    10. Upsert project_interpretations.
    11. Write concept_edges for each relation (skip unknown targets).
    """
    payload = job.get("payload", {})
    resource_id = payload.get("resource_id")
    project_id = payload.get("project_id") or job.get("project_id")

    if not resource_id:
        log.warning("infer_interpretation: payload missing resource_id")
        return {"skipped": "missing_resource_id"}
    if not project_id:
        log.warning("infer_interpretation: payload missing project_id")
        return {"skipped": "missing_project_id"}

    trigger = payload.get("trigger", "unknown")

    # 1. Load resource.
    row = conn.execute(
        """
        SELECT n.title, n.body, rn.content_hash
        FROM nodes n
        JOIN raw_nodes rn ON rn.id = n.id
        WHERE n.id = ?
        """,
        (resource_id,),
    ).fetchone()
    if row is None:
        log.warning(
            "infer_interpretation: resource not found: %s", resource_id
        )
        return {"skipped": "resource_not_found"}

    title = row["title"] or ""
    body = row["body"] or ""
    content_hash = row["content_hash"] or ""

    # 2. Load project profile.
    project_row = conn.execute(
        "SELECT profile_md FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    if project_row is None:
        log.warning(
            "infer_interpretation: project not found: %s", project_id
        )
        return {"skipped": "project_not_found"}
    profile_md = project_row["profile_md"] or ""

    # 3. Top-10 aspects for this project.
    from loci.graph.aspects import AspectRepository

    aspect_repo = AspectRepository(conn)
    top_aspect_pairs = aspect_repo.top_aspects(project_id, limit=10)
    aspect_labels = [label for label, _count in top_aspect_pairs]

    # 4. Last-10 distinct recall queries for this project.
    query_rows = conn.execute(
        """
        SELECT DISTINCT query
        FROM resource_usage_log
        WHERE project_id = ? AND query IS NOT NULL
        ORDER BY used_at DESC
        LIMIT 10
        """,
        (project_id,),
    ).fetchall()
    recent_queries = [r["query"] for r in query_rows]

    # 5. Compute inputs_hash; skip if unchanged.
    from loci.graph.interpretations import ProjectInterpretationRepository

    interp_repo = ProjectInterpretationRepository(conn)
    new_hash = interp_repo.compute_inputs_hash(
        content_hash, profile_md, aspect_labels, recent_queries
    )

    force = payload.get("force", False)
    existing = interp_repo.get(project_id, resource_id)
    if existing is not None and existing.inputs_hash == new_hash and not force:
        log.info(
            "infer_interpretation: inputs unchanged for resource=%s project=%s; skipping",
            resource_id,
            project_id,
        )
        return {"skipped": "inputs_unchanged"}

    # 6. User-gold aspect labels — never overwritten by LLM.
    gold_rows = conn.execute(
        """
        SELECT av.label
        FROM project_resource_aspects pra
        JOIN aspect_vocab av ON av.id = pra.aspect_id
        WHERE pra.project_id = ? AND pra.resource_id = ? AND pra.source = 'user'
        """,
        (project_id, resource_id),
    ).fetchall()
    gold_labels: set[str] = {r["label"] for r in gold_rows}

    # 7. Recent conversation events (up to 20) for context.
    event_rows = conn.execute(
        """
        SELECT text
        FROM conversation_events
        WHERE project_id = ?
        ORDER BY received_at DESC
        LIMIT 20
        """,
        (project_id,),
    ).fetchall()
    conversation_snippets = [r["text"] for r in event_rows]

    # 8. Call LLM.
    try:
        from loci.capture.aspect_suggest import classify_project_interpretation_llm

        output = await classify_project_interpretation_llm(
            text=body,
            title=title,
            existing_vocab=aspect_labels,
            project_profile_md=profile_md,
            recent_queries=recent_queries,
            gold_labels=list(gold_labels),
            prior_summary=existing.summary_md if existing else None,
            conversation_snippets=conversation_snippets,
            settings=settings,
        )
    except ImportError:
        log.warning(
            "infer_interpretation: classify_project_interpretation_llm not yet available; skipping"
        )
        return {"skipped": "llm_error"}
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "infer_interpretation: LLM call failed for resource=%s: %s",
            resource_id,
            exc,
        )
        return {"skipped": "llm_error"}

    # 9. Write project_resource_aspects — skip gold labels.
    from loci.graph.models import now_iso

    ts = now_iso()
    non_gold_scores = [s for s in output.aspects if s.label not in gold_labels]
    aspect_repo.tag_resource_with_propositions(
        project_id=project_id,
        resource_id=resource_id,
        aspect_scores=non_gold_scores,
        source="llm",
    )
    aspects_written = len(non_gold_scores)

    # 10. Upsert project_interpretations.
    from loci.graph.models import ProjectInterpretation

    interp = ProjectInterpretation(
        project_id=project_id,
        resource_id=resource_id,
        summary_md=output.summary_md,
        stance=output.stance or "reference",
        inputs_hash=new_hash,
        model_id=getattr(settings, "rag_model", None),
        generated_at=ts,
    )
    interp_repo.upsert(interp)

    # 11. Write concept_edges for proposed relations.
    edges_written = 0
    for relation in output.relations:
        target_id = relation.target_resource_id
        # Verify target exists in raw_nodes before writing.
        target_exists = conn.execute(
            "SELECT 1 FROM raw_nodes WHERE id = ?", (target_id,)
        ).fetchone()
        if target_exists is None:
            log.info(
                "infer_interpretation: skipping edge to unknown target=%s", target_id
            )
            continue
        # Write project-scoped edge directly (add_edge doesn't expose project_id yet).
        _upsert_project_edge(
            conn,
            src_id=resource_id,
            dst_id=target_id,
            edge_type=relation.edge_type,
            weight=relation.weight,
            metadata={"evidence": relation.evidence} if relation.evidence else None,
            project_id=project_id,
        )
        edges_written += 1

    log.info(
        "infer_interpretation: resource=%s project=%s trigger=%s "
        "aspects=%d edges=%d",
        resource_id,
        project_id,
        trigger,
        aspects_written,
        edges_written,
    )
    return {
        "resource_id": resource_id,
        "project_id": project_id,
        "trigger": trigger,
        "aspects_written": aspects_written,
        "edges_written": edges_written,
        "stance": interp.stance,
        "model": interp.model_id,
    }


def _upsert_project_edge(
    conn: sqlite3.Connection,
    *,
    src_id: str,
    dst_id: str,
    edge_type: str,
    weight: float,
    metadata: dict | None,
    project_id: str,
) -> None:
    """Upsert a project-scoped concept edge directly.

    ConceptEdgeRepository.add_edge() does not yet expose the project_id column,
    so we write to the table directly here. Idempotent on
    (src_id, dst_id, edge_type, project_id).
    """
    import json

    from loci.graph.models import new_id, now_iso

    metadata_json = json.dumps(metadata) if metadata is not None else None
    existing = conn.execute(
        """
        SELECT id FROM concept_edges
        WHERE src_id = ? AND dst_id = ? AND edge_type = ?
          AND project_id IS ?
        """,
        (src_id, dst_id, edge_type, project_id),
    ).fetchone()

    if existing is not None:
        conn.execute(
            """
            UPDATE concept_edges
            SET weight = ?, metadata = ?
            WHERE src_id = ? AND dst_id = ? AND edge_type = ?
              AND project_id IS ?
            """,
            (weight, metadata_json, src_id, dst_id, edge_type, project_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO concept_edges(
                id, src_id, dst_id, edge_type,
                relation_hint, weight, metadata, project_id, created_at
            )
            VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                new_id(),
                src_id,
                dst_id,
                edge_type,
                weight,
                metadata_json,
                project_id,
                now_iso(),
            ),
        )
