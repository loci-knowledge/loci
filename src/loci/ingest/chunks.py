"""Chunk repository — read/write `raw_chunks` and `chunk_vec` rows.

The chunker (`loci.ingest.chunker`) decides where chunk boundaries are; this
module persists the result. Embeddings are written to `chunk_vec` in the
same transaction as the chunk row so the index never gets out of sync with
the parent.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np
import ulid

from loci.embed.local import vec_to_blob
from loci.ingest.chunker import Chunk


@dataclass
class ChunkRow:
    id: str
    raw_id: str
    ord: int
    char_start: int
    char_end: int
    text: str
    section: str | None


def _new_chunk_id() -> str:
    return str(ulid.new())


def write_chunks(
    conn: sqlite3.Connection,
    raw_id: str,
    chunks: list[Chunk],
    embeddings: np.ndarray | None,
) -> list[str]:
    """Insert chunks + their embeddings for a single raw.

    `embeddings` is shape (len(chunks), dim) and must be unit-normalized.
    Pass None to skip the vec write — useful when the embedder failed and
    we still want lex retrieval to work.

    Caller is responsible for the transaction context.
    """
    if not chunks:
        return []
    if embeddings is not None and len(embeddings) != len(chunks):
        raise ValueError(
            f"chunk/embedding count mismatch: {len(chunks)} chunks, "
            f"{len(embeddings)} embeddings",
        )
    chunk_ids: list[str] = []
    for ord_idx, chunk in enumerate(chunks):
        chunk_id = _new_chunk_id()
        conn.execute(
            """
            INSERT INTO raw_chunks(id, raw_id, ord, char_start, char_end,
                                    text, section)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                chunk_id, raw_id, ord_idx, chunk.char_start, chunk.char_end,
                chunk.text, chunk.section,
            ),
        )
        if embeddings is not None:
            conn.execute(
                "INSERT INTO chunk_vec(chunk_id, embedding) VALUES (?, ?)",
                (chunk_id, vec_to_blob(embeddings[ord_idx])),
            )
        chunk_ids.append(chunk_id)
    return chunk_ids


def chunks_for(conn: sqlite3.Connection, raw_id: str) -> list[ChunkRow]:
    rows = conn.execute(
        """
        SELECT id, raw_id, ord, char_start, char_end, text, section
        FROM raw_chunks
        WHERE raw_id = ?
        ORDER BY ord
        """,
        (raw_id,),
    ).fetchall()
    return [
        ChunkRow(
            id=r["id"], raw_id=r["raw_id"], ord=r["ord"],
            char_start=r["char_start"], char_end=r["char_end"],
            text=r["text"], section=r["section"],
        )
        for r in rows
    ]


def get_chunk(conn: sqlite3.Connection, chunk_id: str) -> ChunkRow | None:
    row = conn.execute(
        """
        SELECT id, raw_id, ord, char_start, char_end, text, section
        FROM raw_chunks WHERE id = ?
        """,
        (chunk_id,),
    ).fetchone()
    if row is None:
        return None
    return ChunkRow(
        id=row["id"], raw_id=row["raw_id"], ord=row["ord"],
        char_start=row["char_start"], char_end=row["char_end"],
        text=row["text"], section=row["section"],
    )


def has_chunks(conn: sqlite3.Connection, raw_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM raw_chunks WHERE raw_id = ? LIMIT 1", (raw_id,),
    ).fetchone()
    return row is not None
