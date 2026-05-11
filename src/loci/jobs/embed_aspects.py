"""Job handler: embed all aspect vocab labels that lack stored embeddings.

Processes aspect_vocab rows that have no corresponding row in
aspect_embeddings, encodes them with the process-global embedder, and
stores the resulting unit-normalised float32 vectors.

Skips gracefully if the aspect_embeddings table doesn't exist yet (migration
not run) or if the embedder is unavailable.

Payload: {} (no parameters required)

Returns: {"embedded": N}  where N is the number of new embeddings stored.
"""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)

_BATCH_SIZE = 64


async def handle(job: dict, conn: sqlite3.Connection, settings=None) -> dict:
    """Embed all aspect vocab labels that lack stored embeddings.

    Steps:
    1. Check that aspect_embeddings table exists; skip if not.
    2. Fetch aspect_vocab rows that have no aspect_embeddings row.
    3. Encode their labels in batches of 64 using the global embedder.
    4. Store each via AspectEmbedRepository.store().

    Returns {"embedded": N}.
    """
    # Guard: aspect_embeddings table must exist.
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='aspect_embeddings'"
        )
    }
    if "aspect_embeddings" not in tables:
        log.debug("embed_aspects: aspect_embeddings table not present; skipping")
        return {"embedded": 0}

    # Fetch aspects that haven't been embedded yet.
    rows = conn.execute(
        """
        SELECT av.id, av.label
        FROM aspect_vocab av
        WHERE NOT EXISTS (
            SELECT 1 FROM aspect_embeddings ae WHERE ae.aspect_id = av.id
        )
        ORDER BY av.created_at
        """
    ).fetchall()

    if not rows:
        log.debug("embed_aspects: all aspect labels already embedded")
        return {"embedded": 0}

    try:
        from loci.embed.local import get_embedder  # noqa: PLC0415
        from loci.graph.aspect_embed import AspectEmbedRepository  # noqa: PLC0415
    except ImportError as exc:
        log.warning("embed_aspects: import failed (%s); skipping", exc)
        return {"embedded": 0}

    embedder = get_embedder()
    ae_repo = AspectEmbedRepository(conn)
    model_id = embedder.model_name
    embedded = 0

    # Process in batches.
    for batch_start in range(0, len(rows), _BATCH_SIZE):
        batch = rows[batch_start : batch_start + _BATCH_SIZE]
        aspect_ids = [r["id"] for r in batch]
        labels = [r["label"] for r in batch]

        try:
            vecs = embedder.encode_batch(labels)
        except Exception:  # noqa: BLE001
            log.warning(
                "embed_aspects: encode_batch failed for batch starting at %d; skipping batch",
                batch_start,
                exc_info=True,
            )
            continue

        for aspect_id, vec in zip(aspect_ids, vecs):
            try:
                ae_repo.store(aspect_id, vec, model_id)
                embedded += 1
            except Exception:  # noqa: BLE001
                log.warning(
                    "embed_aspects: store failed for aspect_id=%s; skipping",
                    aspect_id,
                    exc_info=True,
                )

    log.info("embed_aspects: embedded %d aspect labels", embedded)
    return {"embedded": embedded}
