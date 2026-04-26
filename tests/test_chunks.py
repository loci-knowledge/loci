"""Chunk-level retrieval integration tests.

End-to-end: scan a corpus → confirm raw_chunks gets populated → confirm
that chunk-aware vec/lex search returns the right raws + chunk text.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from loci.ingest import scan_path
from loci.ingest.chunks import chunks_for, has_chunks
from loci.retrieve import lex as lex_mod
from loci.retrieve import vec as vec_mod


@pytest.fixture
def long_corpus(tmp_path: Path) -> Path:
    """A corpus with at least one file long enough to chunk into multiple spans."""
    src = tmp_path / "longcorpus"
    src.mkdir()
    # Markdown with three sections, each comfortably > TARGET_CHARS so the
    # chunker will emit one (or more) chunk per section.
    long_md = (
        "# Rotary Embeddings\n\n"
        + ("RoFormer rotates positions into the projections. " * 60)
        + "\n\n# Cross Attention\n\n"
        + ("Cross attention reads keys and values from a different sequence. " * 60)
        + "\n\n# Sinusoidal Positions\n\n"
        + ("Sinusoidal embeddings are added to inputs before the first layer. " * 60)
    )
    (src / "transformer-positions.md").write_text(long_md)
    # And a short markdown file (single chunk).
    (src / "short-note.md").write_text("# Note\n\nA brief note about graph RAG.")
    return src


def test_scan_writes_chunks(conn, fake_embedder, workspace, long_corpus):
    res = scan_path(conn, workspace.id, long_corpus, embedder=fake_embedder)
    assert res.new_raw == 2
    assert res.errors == []

    # Both raws should have at least one chunk.
    raw_ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM nodes WHERE kind = 'raw'",
        ).fetchall()
    ]
    assert len(raw_ids) == 2
    for rid in raw_ids:
        assert has_chunks(conn, rid), f"raw {rid} has no chunks"
    # The long file should produce more than one chunk.
    long_raw = conn.execute(
        "SELECT id FROM nodes WHERE title = 'Rotary Embeddings'",
    ).fetchone()["id"]
    long_chunks = chunks_for(conn, long_raw)
    assert len(long_chunks) >= 2

    # chunk_vec gets populated alongside raw_chunks.
    n_vecs = conn.execute(
        "SELECT COUNT(*) FROM chunk_vec",
    ).fetchone()[0]
    n_chunks = conn.execute(
        "SELECT COUNT(*) FROM raw_chunks",
    ).fetchone()[0]
    assert n_vecs == n_chunks


def test_chunks_fts_indexes_chunk_text(conn, fake_embedder, workspace, long_corpus):
    scan_path(conn, workspace.id, long_corpus, embedder=fake_embedder)
    # Each chunk text should appear in chunks_fts (the trigger does this).
    rows = conn.execute(
        "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ?",
        ("rotary",),
    ).fetchall()
    assert rows, "expected chunks_fts to surface 'rotary'"


def test_lex_search_returns_chunk_handle(conn, fake_embedder, project, workspace, long_corpus):
    scan_path(conn, workspace.id, long_corpus, embedder=fake_embedder)
    hits = lex_mod.search(conn, project.id, "rotary", k=5, kind="raw")
    assert hits
    # The winning hit should carry the chunk_id + chunk_text from chunks_fts.
    top = hits[0]
    assert top.chunk_id is not None
    assert top.chunk_text is not None and "rotary" in top.chunk_text.lower()


def test_chunks_cascade_delete_on_raw(conn, fake_embedder, workspace, long_corpus):
    scan_path(conn, workspace.id, long_corpus, embedder=fake_embedder)
    raw_id = conn.execute(
        "SELECT id FROM nodes WHERE title = 'Rotary Embeddings'",
    ).fetchone()["id"]
    n_chunks_before = conn.execute(
        "SELECT COUNT(*) FROM raw_chunks WHERE raw_id = ?", (raw_id,),
    ).fetchone()[0]
    assert n_chunks_before > 0

    # Deleting the raw should cascade to raw_chunks (FK ON DELETE CASCADE)
    # and the AFTER DELETE trigger on raw_chunks should clean chunk_vec/fts.
    conn.execute("DELETE FROM nodes WHERE id = ?", (raw_id,))
    after = conn.execute(
        "SELECT COUNT(*) FROM raw_chunks WHERE raw_id = ?", (raw_id,),
    ).fetchone()[0]
    assert after == 0
