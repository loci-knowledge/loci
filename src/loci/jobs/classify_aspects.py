"""classify_aspects job — LLM-backed aspect classification for a resource.

Triggered after ingest when a resource has no aspects yet, or after it
accumulates enough usage events to warrant a refinement pass.

Payload shape:
    {
      "resource_id": "<ULID>",   # required
      "project_id":  "<ULID>"    # required
    }
"""

from __future__ import annotations

import logging
import sqlite3

from loci.graph.aspects import AspectRepository

log = logging.getLogger(__name__)


async def handle_classify_aspects(job: dict, conn: sqlite3.Connection, settings) -> dict:
    """Classify aspects for a resource using the LLM.

    Steps:
    1. Load resource text from nodes + raw_nodes (body column holds extracted text).
    2. Get existing aspect vocab for the project.
    3. Call classify_aspects_llm() from capture/aspect_suggest.py.
    4. Write inferred aspects to resource_aspects with source="inferred".
    5. Mark any new vocab terms in aspect_vocab with auto_inferred=1 (done
       automatically by AspectRepository.tag_resource → ensure_aspect("inferred")).
    """
    payload = job.get("payload", {})
    resource_id = payload.get("resource_id")
    project_id = payload.get("project_id")

    if not resource_id:
        raise ValueError("classify_aspects: payload missing resource_id")
    if not project_id:
        raise ValueError("classify_aspects: payload missing project_id")

    # 1. Load resource text and title.
    row = conn.execute(
        """
        SELECT n.title, n.body
        FROM nodes n
        JOIN raw_nodes r ON r.node_id = n.id
        WHERE n.id = ? AND n.kind = 'raw'
        """,
        (resource_id,),
    ).fetchone()

    if row is None:
        log.warning("classify_aspects: resource not found: %s", resource_id)
        return {"skipped": True, "reason": "resource not found"}

    title = row["title"] or ""
    text = row["body"] or ""

    if not text.strip() and not title.strip():
        log.info("classify_aspects: resource %s has no text; skipping", resource_id)
        return {"skipped": True, "reason": "no text content"}

    # 2. Get existing aspect vocab for the project.
    aspects_repo = AspectRepository(conn)
    existing_aspects = aspects_repo.list_vocab(project_id=project_id)
    existing_vocab = [a.label for a in existing_aspects]

    # 3. Call LLM classifier.
    from loci.capture.aspect_suggest import classify_aspects_llm

    try:
        pairs = await classify_aspects_llm(
            text=text,
            title=title,
            existing_vocab=existing_vocab,
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("classify_aspects: LLM call failed for %s", resource_id)
        return {"skipped": False, "error": str(exc), "aspects_written": 0}

    if not pairs:
        log.info("classify_aspects: no aspects returned for %s", resource_id)
        return {"skipped": False, "aspects_written": 0}

    # 4 & 5. Write inferred aspects; ensure_aspect(source="inferred") sets
    # auto_inferred=1 on any new vocab term.
    labels = [label for label, _conf in pairs]
    # Use the per-label confidence values rather than a flat 1.0.
    ts = _now_iso()
    for label, confidence in pairs:
        aspect = aspects_repo.ensure_aspect(label, source="inferred")
        conn.execute(
            """
            INSERT INTO resource_aspects(resource_id, aspect_id, confidence, source, created_at)
            VALUES (?, ?, ?, 'inferred', ?)
            ON CONFLICT(resource_id, aspect_id) DO UPDATE SET
                confidence = excluded.confidence,
                source = excluded.source
            """,
            (resource_id, aspect.id, confidence, ts),
        )
        aspects_repo.touch_last_used(label)

    log.info(
        "classify_aspects: wrote %d aspects for resource %s", len(pairs), resource_id
    )
    return {
        "skipped": False,
        "aspects_written": len(pairs),
        "labels": labels,
        "model": getattr(settings, "rag_model", "unknown"),
    }


def _now_iso() -> str:
    from loci.graph.models import now_iso
    return now_iso()
