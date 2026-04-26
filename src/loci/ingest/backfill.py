"""One-shot backfill: chunk + embed any RawNode that pre-dates 0002_chunks.

Use when an existing database has raws stored as single-vector files (the
0001 model) and you've just applied 0002_chunks.sql. The retrieval layer
falls back to `node_vec` for un-chunked raws so nothing breaks before
backfill — but precision improves a lot once chunks are in place.

Idempotent: a raw that already has chunks is skipped. Re-runnable.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from loci.db.connection import transaction
from loci.embed.local import Embedder, get_embedder
from loci.ingest.chunker import chunk_doc
from loci.ingest.chunks import raws_missing_chunks, write_chunks

log = logging.getLogger(__name__)


@dataclass
class BackfillResult:
    raws_processed: int = 0
    chunks_written: int = 0
    skipped_empty: int = 0
    errors: list[str] = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


def backfill_chunks(
    conn: sqlite3.Connection,
    project_id: str | None = None,
    *,
    embedder: Embedder | None = None,
    batch_size: int = 32,
) -> BackfillResult:
    """Walk raws missing chunks; chunk + embed each; persist.

    Pass `project_id` to scope to one project; omit to backfill the whole DB.
    """
    embedder = embedder or get_embedder()
    result = BackfillResult()
    pending = raws_missing_chunks(conn, project_id)
    if not pending:
        return result

    log.info("backfill: chunking %d raws", len(pending))

    # We process raws one at a time but embed each raw's chunks in a single
    # batched call. Cross-raw batching would help throughput but complicates
    # the per-raw transactional write — not worth it for the one-shot path.
    for raw_id, body, subkind in pending:
        try:
            chunks = chunk_doc(body, subkind)
        except Exception as exc:  # noqa: BLE001
            log.exception("backfill: chunker failed for %s", raw_id)
            result.errors.append(f"{raw_id}: chunker: {exc}")
            continue
        if not chunks:
            result.skipped_empty += 1
            continue
        # Embed chunks in batches.
        texts = [c.text[:8192] for c in chunks]
        try:
            vecs = embedder.encode_batch(texts)
        except Exception as exc:  # noqa: BLE001
            log.exception("backfill: embed failed for %s", raw_id)
            result.errors.append(f"{raw_id}: embed: {exc}")
            continue
        try:
            with transaction(conn):
                write_chunks(conn, raw_id, chunks, vecs)
        except Exception as exc:  # noqa: BLE001
            log.exception("backfill: write failed for %s", raw_id)
            result.errors.append(f"{raw_id}: write: {exc}")
            continue
        result.raws_processed += 1
        result.chunks_written += len(chunks)
        if batch_size and result.raws_processed % batch_size == 0:
            log.info("backfill: %d raws, %d chunks so far",
                     result.raws_processed, result.chunks_written)

    return result
