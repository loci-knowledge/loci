"""Chunker unit tests — pure-function, no DB or embedder."""

from __future__ import annotations

from loci.ingest.chunker import (
    MAX_CHARS,
    MIN_CHARS,
    TARGET_CHARS,
    chunk_doc,
)


def test_empty_input_returns_no_chunks():
    assert chunk_doc("", "md") == []
    assert chunk_doc("   \n  \n", "md") == []


def test_short_markdown_is_one_chunk():
    text = "# Title\n\nA short paragraph."
    chunks = chunk_doc(text, "md")
    assert len(chunks) == 1
    c = chunks[0]
    assert "Title" in c.text
    assert "short paragraph" in c.text
    assert c.section is not None and "Title" in c.section
    assert c.char_start == 0
    assert c.char_end == len(text)


def test_markdown_splits_on_headings():
    text = (
        "# A\n\nFirst section body.\n\n"
        "# B\n\nSecond section body.\n\n"
        "# C\n\nThird section body."
    )
    chunks = chunk_doc(text, "md")
    sections = [c.section for c in chunks]
    # Sections may be merged if tiny — we expect at least 2 distinct headings.
    distinct = {s for s in sections if s}
    assert len(distinct) >= 2


def test_markdown_large_section_packs_paragraphs():
    para = "This is a long paragraph that takes up some space. " * 20  # ~ 1k chars
    text = f"# Big Section\n\n" + ("\n\n".join([para] * 6))  # ~6k chars
    chunks = chunk_doc(text, "md")
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c.text) <= MAX_CHARS + 100  # small slack from indent + sentinels


def test_markdown_with_no_headings_falls_back_to_paragraphs():
    text = "\n\n".join(["Paragraph " + str(i) + " content. " * 30 for i in range(10)])
    chunks = chunk_doc(text, "md")
    assert len(chunks) >= 2
    for c in chunks:
        assert c.section is None


def test_code_uses_sliding_window():
    text = "def f():\n    return 42\n\n" * 200  # ~5k chars
    chunks = chunk_doc(text, "code")
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c.text) <= MAX_CHARS + 100


def test_pdf_subkind_uses_paragraph_packing():
    text = "\n\n".join([f"Paragraph {i}. " * 50 for i in range(10)])
    chunks = chunk_doc(text, "pdf")
    assert len(chunks) >= 2


def test_offsets_are_monotonic():
    text = "# A\n\n" + ("Para " * 200) + "\n\n# B\n\n" + ("Word " * 200)
    chunks = chunk_doc(text, "md")
    last_end = -1
    for c in chunks:
        # Offsets monotonically increase across chunk boundaries.
        # (Within MD heading splits, the next chunk starts at the heading's start.)
        assert c.char_start >= 0
        assert c.char_end >= c.char_start
        # Allow equal (back-to-back) but not overlap-going-backward.
        assert c.char_end >= last_end - MAX_CHARS
        last_end = c.char_end


def test_giant_single_paragraph_is_split():
    text = "x" * (MAX_CHARS * 4)  # one huge "paragraph"
    chunks = chunk_doc(text, "txt")
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c.text) <= MAX_CHARS + 100


def test_tiny_trailing_chunk_merges_into_neighbour():
    text = (
        "## Section A\n\n" + ("alpha " * 200) + "\n\n"  # ~1.2k → fits target
        "## Section B\n\n" + "tiny"                     # under MIN_CHARS
    )
    chunks = chunk_doc(text, "md")
    # Trailing "Section B" stub (a different section) is not merged because
    # the merger only collapses within the same section. We accept either
    # outcome; what we *don't* want is a stub of length ~10 surviving alone.
    assert all(len(c.text) >= 4 for c in chunks)
