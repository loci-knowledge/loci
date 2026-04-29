"""Capture pipeline — unified entry point for ingesting a resource.

Orchestrates:
  1. Detect input type (URL, file path, or raw text)
  2. Fetch/read content → extract text (reuse loci.ingest.extractors)
  3. Hash → dedup check against the project's existing resources
  4. Write raw_nodes + raw_chunks + chunk_vec + resource_provenance
  5. Run folder_suggest + aspect_suggest
  6. Return CaptureResult

The caller decides what to do with folder_suggestions and aspect_suggestions
(accept, present to user, discard). This module never writes resource_aspects
or concept_edges — those are handled by the background jobs.

URL handling:
  - ArXiv abstract pages (arxiv.org/abs/<id>) are rewritten to the PDF URL so
    the PDF extractor path is used instead of the HTML path.
  - All other URLs are fetched with httpx and saved to a temp file, then
    dispatched through the normal file extractor.
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_ARXIV_ABS_PREFIX = "arxiv.org/abs/"
_ARXIV_PDF_PREFIX = "arxiv.org/pdf/"


@dataclass
class CaptureResult:
    resource_id: str
    title: str
    is_duplicate: bool
    folder_suggestions: list[tuple[str, float]]  # (folder_path, score) top-3
    aspect_suggestions: list[str]                # top-5 aspect labels
    existing_folder: str | None                  # if duplicate, the existing folder
    existing_aspects: list[str]                  # if duplicate, existing aspect labels


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def ingest_url(
    url: str,
    context_text: str | None,
    project_id: str,
    conn: sqlite3.Connection,
) -> CaptureResult:
    """Fetch a URL, extract, ingest, and return CaptureResult."""
    import httpx

    fetch_url = _maybe_arxiv_pdf(url)
    log.debug("ingest_url: fetching %s", fetch_url)

    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        resp = await client.get(fetch_url)
        resp.raise_for_status()
        content_bytes = resp.content
        content_type = resp.headers.get("content-type", "")

    # Determine a file extension from Content-Type or the URL itself.
    suffix = _suffix_from_content_type(content_type) or _suffix_from_url(fetch_url)

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content_bytes)
        tmp_path = Path(tmp.name)

    try:
        result = await _ingest_path(
            path=tmp_path,
            context_text=context_text,
            project_id=project_id,
            conn=conn,
            source_url=url,
            saved_via="mcp",
        )
    finally:
        tmp_path.unlink(missing_ok=True)

    return result


async def ingest_file(
    path: str | Path,
    context_text: str | None,
    project_id: str,
    conn: sqlite3.Connection,
) -> CaptureResult:
    """Ingest a local file and return CaptureResult."""
    return await _ingest_path(
        path=Path(path),
        context_text=context_text,
        project_id=project_id,
        conn=conn,
        source_url=None,
        saved_via="cli",
    )


async def ingest_text(
    text: str,
    title: str,
    context_text: str | None,
    project_id: str,
    conn: sqlite3.Connection,
) -> CaptureResult:
    """Ingest raw text (e.g. a pasted note) and return CaptureResult."""
    import asyncio

    # Run the synchronous work in a thread executor to avoid blocking the event loop.
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        _ingest_text_sync,
        text, title, context_text, project_id, conn,
    )


# ---------------------------------------------------------------------------
# Core sync implementation
# ---------------------------------------------------------------------------


async def _ingest_path(
    path: Path,
    context_text: str | None,
    project_id: str,
    conn: sqlite3.Connection,
    source_url: str | None,
    saved_via: str,
) -> CaptureResult:
    """Hash, dedup, extract, chunk, embed, and write a single file."""
    import asyncio

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        _ingest_path_sync,
        path, context_text, project_id, conn, source_url, saved_via,
    )


def _ingest_path_sync(
    path: Path,
    context_text: str | None,
    project_id: str,
    conn: sqlite3.Connection,
    source_url: str | None,
    saved_via: str,
) -> CaptureResult:
    from loci.capture.aspect_suggest import suggest_aspects_sync
    from loci.capture.folder_suggest import suggest_folders
    from loci.embed.local import get_embedder
    from loci.graph.aspects import AspectRepository
    from loci.graph.models import RawNode
    from loci.graph.sources import SourceRepository
    from loci.ingest.chunker import chunk_doc
    from loci.ingest.content_hash import hash_file, store_blob
    from loci.ingest.extractors import extract

    # --- Hash + dedup ---
    full_hash, trunc_hash, size = hash_file(path)

    src_repo = SourceRepository(conn)
    existing = src_repo.get_by_hash(trunc_hash)
    if existing is not None:
        # Already in DB — check if it's already in this project.
        existing_folder, existing_aspects = _existing_provenance_and_aspects(
            conn, existing.id
        )
        return CaptureResult(
            resource_id=existing.id,
            title=existing.title,
            is_duplicate=True,
            folder_suggestions=[],
            aspect_suggestions=[],
            existing_folder=existing_folder,
            existing_aspects=existing_aspects,
        )

    # --- Extract text ---
    extracted = extract(path)
    if extracted is None:
        raise ValueError(f"Cannot extract text from {path}")

    # --- Derive title ---
    title = _derive_title(path, extracted.text, extracted.subkind)

    # --- Chunk + embed ---
    chunks = chunk_doc(extracted.text, extracted.subkind)
    embedder = get_embedder()
    chunk_vecs = None
    if chunks:
        chunk_texts = [_embed_text(path, c) for c in chunks]
        try:
            chunk_vecs = embedder.encode_batch(chunk_texts)
        except Exception:  # noqa: BLE001
            log.warning("ingest_path: embedding failed for %s; storing text only", path)

    # --- Write to DB ---
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"Cannot read file {path}: {exc}") from exc

    store_blob(full_hash, raw_bytes)

    node = RawNode(
        subkind=extracted.subkind,
        title=title,
        body=extracted.text,
        content_hash=trunc_hash,
        canonical_path=str(path),
        mime=extracted.mime,
        size_bytes=size,
    )
    src_repo.insert(node, chunks=chunks, chunk_embeddings=chunk_vecs)
    _write_provenance(conn, node.id, source_url, folder=None, saved_via=saved_via, context_text=context_text)

    # --- Suggest ---
    aspect_repo = AspectRepository(conn)
    existing_vocab = [a.label for a in aspect_repo.list_vocab(project_id=project_id)]

    folder_suggestions = suggest_folders(
        title=title,
        abstract_text=extracted.text[:500],
        conn=conn,
        project_id=project_id,
    )
    aspect_suggestions = suggest_aspects_sync(
        text=extracted.text,
        existing_vocab=existing_vocab,
    )

    return CaptureResult(
        resource_id=node.id,
        title=title,
        is_duplicate=False,
        folder_suggestions=folder_suggestions,
        aspect_suggestions=aspect_suggestions,
        existing_folder=None,
        existing_aspects=[],
    )


def _ingest_text_sync(
    text: str,
    title: str,
    context_text: str | None,
    project_id: str,
    conn: sqlite3.Connection,
) -> CaptureResult:
    """Ingest raw text, writing it as a 'txt' subkind raw node."""
    from loci.capture.aspect_suggest import suggest_aspects_sync
    from loci.capture.folder_suggest import suggest_folders
    from loci.embed.local import get_embedder
    from loci.graph.aspects import AspectRepository
    from loci.graph.models import RawNode
    from loci.graph.sources import SourceRepository
    from loci.ingest.chunker import chunk_doc
    from loci.ingest.content_hash import hash_bytes, store_blob

    text_bytes = text.encode("utf-8")
    full_hash, trunc_hash = hash_bytes(text_bytes)

    src_repo = SourceRepository(conn)
    existing = src_repo.get_by_hash(trunc_hash)
    if existing is not None:
        existing_folder, existing_aspects = _existing_provenance_and_aspects(
            conn, existing.id
        )
        return CaptureResult(
            resource_id=existing.id,
            title=existing.title,
            is_duplicate=True,
            folder_suggestions=[],
            aspect_suggestions=[],
            existing_folder=existing_folder,
            existing_aspects=existing_aspects,
        )

    chunks = chunk_doc(text, "txt")
    embedder = get_embedder()
    chunk_vecs = None
    if chunks:
        try:
            chunk_vecs = embedder.encode_batch([c.text for c in chunks])
        except Exception:  # noqa: BLE001
            log.warning("ingest_text: embedding failed for text node '%s'", title)

    store_blob(full_hash, text_bytes)

    node = RawNode(
        subkind="txt",
        title=title,
        body=text,
        content_hash=trunc_hash,
        canonical_path="",
        mime="text/plain",
        size_bytes=len(text_bytes),
    )
    src_repo.insert(node, chunks=chunks, chunk_embeddings=chunk_vecs)
    _write_provenance(conn, node.id, source_url=None, folder=None, saved_via="mcp", context_text=context_text)

    aspect_repo = AspectRepository(conn)
    existing_vocab = [a.label for a in aspect_repo.list_vocab(project_id=project_id)]

    folder_suggestions = suggest_folders(
        title=title,
        abstract_text=text[:500],
        conn=conn,
        project_id=project_id,
    )
    aspect_suggestions = suggest_aspects_sync(
        text=text,
        existing_vocab=existing_vocab,
    )

    return CaptureResult(
        resource_id=node.id,
        title=title,
        is_duplicate=False,
        folder_suggestions=folder_suggestions,
        aspect_suggestions=aspect_suggestions,
        existing_folder=None,
        existing_aspects=[],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _maybe_arxiv_pdf(url: str) -> str:
    """Rewrite arxiv abstract URLs to PDF URLs for better extraction."""
    # Strip protocol for pattern matching
    stripped = url.removeprefix("https://").removeprefix("http://")
    if stripped.startswith(_ARXIV_ABS_PREFIX):
        arxiv_id = stripped[len(_ARXIV_ABS_PREFIX):]
        return f"https://{_ARXIV_PDF_PREFIX}{arxiv_id}"
    return url


def _suffix_from_content_type(content_type: str) -> str:
    """Map a Content-Type header value to a file suffix."""
    ct = content_type.lower().split(";")[0].strip()
    _MAP = {
        "application/pdf": ".pdf",
        "text/html": ".html",
        "text/markdown": ".md",
        "text/plain": ".txt",
    }
    return _MAP.get(ct, "")


def _suffix_from_url(url: str) -> str:
    """Infer a file suffix from the URL path."""
    path = url.split("?")[0].rstrip("/")
    for ext in (".pdf", ".html", ".htm", ".md", ".txt"):
        if path.endswith(ext):
            return ext
    return ".html"


def _derive_title(path: Path, text: str, subkind: str) -> str:
    if subkind in {"md", "txt", "transcript"}:
        for line in text.splitlines():
            line = line.strip().lstrip("# ").strip()
            if line:
                return line[:200]
    return path.stem or path.name


def _embed_text(path: Path, chunk) -> str:
    """Build embedding input for a chunk (matches pipeline.py convention)."""
    parts = [path.stem]
    if chunk.section:
        parts.append(chunk.section)
    prefix = " :: ".join(parts) + "\n\n"
    return (prefix + chunk.text)[:8192]


def _write_provenance(
    conn: sqlite3.Connection,
    resource_id: str,
    source_url: str | None,
    folder: str | None,
    saved_via: str,
    context_text: str | None,
) -> None:
    from loci.graph.models import now_iso
    conn.execute(
        """
        INSERT OR REPLACE INTO resource_provenance
            (resource_id, source_url, folder, saved_via, context_text, captured_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (resource_id, source_url, folder, saved_via, context_text, now_iso()),
    )


def _existing_provenance_and_aspects(
    conn: sqlite3.Connection,
    resource_id: str,
) -> tuple[str | None, list[str]]:
    """Return (folder, [aspect_labels]) for an already-ingested resource."""
    row = conn.execute(
        "SELECT folder FROM resource_provenance WHERE resource_id = ?",
        (resource_id,),
    ).fetchone()
    folder = row["folder"] if row else None

    rows = conn.execute(
        """
        SELECT av.label
        FROM resource_aspects ra
        JOIN aspect_vocab av ON av.id = ra.aspect_id
        WHERE ra.resource_id = ?
        ORDER BY ra.confidence DESC
        """,
        (resource_id,),
    ).fetchall()
    aspects = [r["label"] for r in rows]
    return folder, aspects
