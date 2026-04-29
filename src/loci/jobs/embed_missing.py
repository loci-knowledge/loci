"""embed_missing job — embed raw nodes that have no entry in node_vec.

This is the v2 replacement for the old reembed/backfill handlers. It operates
only on raw nodes (not interpretation nodes), loading the local sentence-
transformers model and writing whole-file vectors to node_vec.

Payload shape:
    {
      "project_id":  "<ULID>",   # optional — scope to a project
      "batch_size":  32          # optional override
    }

If `project_id` is not in the payload the job falls back to `job["project_id"]`,
and if that is also None it processes *all* unembedded raw nodes in the DB.
"""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)

_DEFAULT_BATCH_SIZE = 32


async def handle_embed_missing(job: dict, conn: sqlite3.Connection, settings) -> dict:
    """Embed all raw nodes that have no node_vec entry.

    Steps:
    1. Query for raw node IDs (and body text) with no node_vec row.
    2. Load the local embedder (sentence-transformers / BGE model).
    3. Encode in batches and write to node_vec.
    """
    payload = job.get("payload", {})
    project_id = payload.get("project_id") or job.get("project_id")
    batch_size = int(payload.get("batch_size", _DEFAULT_BATCH_SIZE))

    # 1. Find raw nodes with missing embeddings.
    if project_id:
        rows = conn.execute(
            """
            SELECT n.id, n.title, n.body
            FROM nodes n
            JOIN raw_nodes r ON r.node_id = n.id
            JOIN project_effective_members pm ON pm.node_id = n.id
            WHERE pm.project_id = ?
              AND n.kind = 'raw'
              AND NOT EXISTS (
                  SELECT 1 FROM node_vec nv WHERE nv.node_id = n.id
              )
            ORDER BY n.created_at DESC
            """,
            (project_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT n.id, n.title, n.body
            FROM nodes n
            JOIN raw_nodes r ON r.node_id = n.id
            WHERE n.kind = 'raw'
              AND NOT EXISTS (
                  SELECT 1 FROM node_vec nv WHERE nv.node_id = n.id
              )
            ORDER BY n.created_at DESC
            """,
        ).fetchall()

    if not rows:
        log.info("embed_missing: no unembedded raw nodes found")
        return {"embedded": 0, "skipped": True, "reason": "nothing to embed"}

    total = len(rows)
    log.info("embed_missing: found %d unembedded raw nodes", total)

    # 2. Load embedder.
    try:
        from loci.embed.local import get_embedder, vec_to_blob
        embedder = get_embedder()
    except Exception as exc:  # noqa: BLE001
        log.exception("embed_missing: embedder load failed")
        return {"embedded": 0, "error": str(exc)}

    # 3. Encode in batches and write.
    embedded = 0
    for start in range(0, total, batch_size):
        batch = rows[start: start + batch_size]
        texts = [
            "\n\n".join(part for part in [row["title"], row["body"]] if part).strip()
            or "(empty)"
            for row in batch
        ]
        try:
            vecs = embedder.encode_batch(texts)
        except Exception as exc:  # noqa: BLE001
            log.warning("embed_missing: batch encode failed at offset %d: %s", start, exc)
            continue

        for row, vec in zip(batch, vecs, strict=False):
            node_id = row["id"]
            blob = vec_to_blob(vec)
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO node_vec(node_id, embedding) VALUES (?, ?)",
                    (node_id, blob),
                )
                embedded += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("embed_missing: write failed for node %s: %s", node_id, exc)

        log.debug("embed_missing: embedded %d/%d so far", embedded, total)

    log.info("embed_missing: done — embedded %d nodes", embedded)
    return {"embedded": embedded, "total_found": total}
