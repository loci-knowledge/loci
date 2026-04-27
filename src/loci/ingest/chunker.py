"""Span-level chunker for RawNodes.

Why chunks: graph-RAG citation precision degrades sharply when a [Cn] marker
points at a whole 50-page PDF. The LLM has no idea which span actually
supports the claim, and neither does the user reading the citation. KG2RAG
(Zhu et al., 2025) and most recent graph-RAG work retrieve at chunk
granularity for this reason.

Strategy per subkind:
  md         — split on `^#{1,6} ` headings; sub-pack large sections by
               paragraph until the chunk hits TARGET_CHARS.
  code       — sliding window with overlap (no language-aware splitting yet).
  pdf, html  — paragraph-packing (extracted text is already de-laid-out).
  txt,
  transcript

Output: a list of `Chunk(text, char_start, char_end, section)`.

Each chunk's text is embedded + FTS-indexed independently. Char offsets are
stored so the UI can highlight the span inside the original raw body.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

# Soft target — chunks aim for this size; FTS/embedder work well around 250–400 tokens.
TARGET_CHARS = 1200
# Hard cap — single chunk never exceeds this. Prevents one giant paragraph
# from blowing the embedder context.
MAX_CHARS = 2000
# Floor — chunks below this are merged into the next one (avoids tiny dust).
MIN_CHARS = 200
# Sliding-window overlap (used for code + paragraph hard-splits).
OVERLAP_CHARS = 150


@dataclass
class Chunk:
    text: str
    char_start: int
    char_end: int
    section: str | None = None


def chunk_doc(text: str, subkind: str) -> list[Chunk]:
    """Split a document into retrievable spans. Subkind picks the strategy."""
    text = text or ""
    if not text.strip():
        return []
    if subkind == "md":
        chunks = list(_chunk_markdown(text))
    elif subkind == "code":
        chunks = list(_chunk_sliding(text, section=None))
    else:
        # pdf, html, txt, transcript, image-with-OCR-text → paragraph packing
        chunks = list(_pack_paragraphs(text, base_offset=0, section=None))
    if not chunks:
        # Pathological input (e.g. one giant line, no whitespace): fall back
        # to a hard sliding split so we always emit at least one chunk.
        chunks = list(_chunk_sliding(text, section=None))
    return _merge_tiny(chunks)


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)


def _chunk_markdown(text: str) -> Iterator[Chunk]:
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        yield from _pack_paragraphs(text, base_offset=0, section=None)
        return

    spans: list[tuple[int, int, str | None]] = []
    # Preamble before the first heading
    first = matches[0].start()
    if first > 0 and text[:first].strip():
        spans.append((0, first, None))
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        heading = m.group(0).strip()
        spans.append((start, end, heading))

    for start, end, heading in spans:
        section_text = text[start:end]
        if len(section_text) <= MAX_CHARS:
            stripped = section_text.strip()
            if stripped:
                yield Chunk(
                    text=stripped, char_start=start, char_end=end, section=heading,
                )
        else:
            yield from _pack_paragraphs(
                section_text, base_offset=start, section=heading,
            )


# ---------------------------------------------------------------------------
# Paragraph packing
# ---------------------------------------------------------------------------

_PARA_SPLIT_RE = re.compile(r"\n[ \t]*\n")


def _iter_paragraphs(text: str) -> Iterator[tuple[int, int, str]]:
    """Yield (start, end, text) for each non-empty paragraph."""
    pos = 0
    for m in _PARA_SPLIT_RE.finditer(text):
        para = text[pos:m.start()]
        if para.strip():
            yield (pos, m.start(), para)
        pos = m.end()
    tail = text[pos:]
    if tail.strip():
        yield (pos, len(text), tail)


def _pack_paragraphs(
    text: str, *, base_offset: int, section: str | None,
) -> Iterator[Chunk]:
    """Greedy pack paragraphs to ~TARGET_CHARS, hard-cap at MAX_CHARS."""
    paras = list(_iter_paragraphs(text))
    if not paras:
        return

    buf = ""
    buf_start = paras[0][0] + base_offset
    buf_end = buf_start

    for para_start, para_end, para in paras:
        global_start = para_start + base_offset
        global_end = para_end + base_offset

        # Single paragraph too big — emit current buffer, then hard-split it.
        if not buf and len(para) > MAX_CHARS:
            yield from _split_long(para, base_offset=global_start, section=section)
            continue

        candidate = (buf + "\n\n" + para) if buf else para
        if buf and len(candidate) > MAX_CHARS:
            yield Chunk(
                text=buf.strip(), char_start=buf_start,
                char_end=buf_end, section=section,
            )
            buf = para
            buf_start = global_start
            buf_end = global_end
        else:
            buf = candidate
            if not buf_start or not buf:
                buf_start = global_start
            buf_end = global_end

        if len(buf) >= TARGET_CHARS:
            yield Chunk(
                text=buf.strip(), char_start=buf_start,
                char_end=buf_end, section=section,
            )
            buf = ""

    if buf and buf.strip():
        yield Chunk(
            text=buf.strip(), char_start=buf_start,
            char_end=buf_end, section=section,
        )


def _split_long(
    text: str, *, base_offset: int, section: str | None,
) -> Iterator[Chunk]:
    """Sliding-window split for a paragraph too large to fit MAX_CHARS."""
    n = len(text)
    step = max(1, TARGET_CHARS - OVERLAP_CHARS)
    start = 0
    while start < n:
        end = min(start + TARGET_CHARS, n)
        sub = text[start:end].strip()
        if sub:
            yield Chunk(
                text=sub,
                char_start=base_offset + start,
                char_end=base_offset + end,
                section=section,
            )
        if end >= n:
            break
        start += step


# ---------------------------------------------------------------------------
# Code (sliding window)
# ---------------------------------------------------------------------------


def _chunk_sliding(text: str, *, section: str | None) -> Iterator[Chunk]:
    n = len(text)
    if n <= MAX_CHARS:
        yield Chunk(text=text.strip(), char_start=0, char_end=n, section=section)
        return
    step = max(1, TARGET_CHARS - OVERLAP_CHARS)
    start = 0
    while start < n:
        end = min(start + TARGET_CHARS, n)
        sub = text[start:end]
        if sub.strip():
            yield Chunk(
                text=sub, char_start=start, char_end=end, section=section,
            )
        if end >= n:
            break
        start += step


# ---------------------------------------------------------------------------
# Tiny-chunk merger (post-pass)
# ---------------------------------------------------------------------------


def _merge_tiny(chunks: list[Chunk]) -> list[Chunk]:
    """Collapse runs of tiny chunks into their successor.

    The packing logic can leave a stub at the end (or after a heading-only
    section) that's smaller than MIN_CHARS. Such chunks rarely carry enough
    signal for retrieval; merging them into the next chunk keeps the boundary
    decision for the chunker, not the model.
    """
    if not chunks:
        return chunks
    out: list[Chunk] = []
    for c in chunks:
        if out and len(out[-1].text) < MIN_CHARS and out[-1].section == c.section:
            prev = out[-1]
            merged = Chunk(
                text=(prev.text + "\n\n" + c.text).strip(),
                char_start=prev.char_start,
                char_end=c.char_end,
                section=prev.section,
            )
            out[-1] = merged
        else:
            out.append(c)
    return out
