"""Ingest orchestrator.

Walks a path → hashes → dedups → extracts → batch-embeds → writes `RawNode`s
and workspace memberships. Returns a summary describing what happened.

The pipeline is workspace-scoped: each scan run records raw nodes as members
of the workspace (not the project directly). Projects receive those nodes
through the project_effective_members view by virtue of being linked to the
workspace.

The loop is structured to be **batch-friendly for embedding**: we collect a
batch of newly-extracted text bodies and call `Embedder.encode_batch()` once
per batch, rather than one model call per file. Embedding is the slow step,
not extraction or hashing.

Idempotency: a file already present in `raw_nodes` (by content_hash) is not
re-extracted. We DO add it to the workspace membership if it's not there yet —
the same paper can join multiple workspaces without re-ingesting.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from loci.embed.local import Embedder, get_embedder
from loci.graph.models import RawNode
from loci.graph.nodes import NodeRepository
from loci.graph.workspaces import WorkspaceRepository
from loci.ingest.chunker import Chunk, chunk_doc
from loci.ingest.content_hash import hash_file, store_blob
from loci.ingest.extractors import ExtractedDoc, extract
from loci.ingest.walker import walk

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    scanned: int = 0
    new_raw: int = 0           # files that became fresh RawNodes
    deduped: int = 0           # files already present in raw_nodes
    skipped: int = 0           # unsupported / unreadable / empty
    members_added: int = 0     # workspace_membership rows newly inserted
    errors: list[str] = field(default_factory=list)


@dataclass
class _Pending:
    """A file that's been hashed + extracted + chunked, waiting to be embedded + written."""
    path: Path
    full_hash: str
    trunc_hash: str
    size: int
    raw_bytes: bytes
    extracted: ExtractedDoc
    chunks: list[Chunk] = field(default_factory=list)


class IngestPipeline:
    """Coordinates a single ingest run for a workspace.

    Construct once per scan; not thread-safe (it batches accumulator state).
    Concurrent scans on the same workspace are fine *between* pipeline instances
    because all writes go through atomic transactions in the repos.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        workspace_id: str,
        *,
        embedder: Embedder | None = None,
        embed_batch_size: int = 32,
    ) -> None:
        self.conn = conn
        self.workspace_id = workspace_id
        self.nodes = NodeRepository(conn)
        self.workspaces = WorkspaceRepository(conn)
        self._embedder = embedder
        self.embed_batch_size = embed_batch_size

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = get_embedder()
        return self._embedder

    # -----------------------------------------------------------------------
    # Public entry point
    # -----------------------------------------------------------------------

    def scan(self, root: Path) -> IngestResult:
        """Walk `root`, ingest everything new, return a summary."""
        result = IngestResult()
        batch: list[_Pending] = []
        # Track hashes staged in the current batch but not yet written to DB.
        # Without this, duplicate files in the same walk (same content in two
        # locations) both pass find_raw_by_hash → batch → second INSERT fails
        # with UNIQUE constraint on raw_nodes.content_hash.
        batch_hashes: set[str] = set()
        for path in walk(root):
            result.scanned += 1
            try:
                outcome = self._stage_file(path, batch_hashes)
            except Exception as exc:  # noqa: BLE001
                msg = f"{path}: {exc}"
                log.exception("ingest staging failed")
                result.errors.append(msg)
                continue
            if outcome is None:
                result.skipped += 1
                continue
            if isinstance(outcome, _DedupOutcome):
                result.deduped += 1
                if outcome.added_membership:
                    result.members_added += 1
                continue
            batch.append(outcome)
            if len(batch) >= self.embed_batch_size:
                self._flush_batch(batch, result)
                batch = []
                batch_hashes.clear()
        if batch:
            self._flush_batch(batch, result)
        self.workspaces.touch(self.workspace_id)
        return result

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    def _stage_file(
        self, path: Path, batch_hashes: set[str] | None = None,
    ) -> _Pending | _DedupOutcome | None:
        """Per-file pre-embed work: hash → dedup check → extract → return pending."""
        full_hash, trunc_hash, size = hash_file(path)
        # Check both the DB and the in-flight batch so duplicate files in the
        # same directory scan don't cause a UNIQUE constraint failure on write.
        if batch_hashes is not None and trunc_hash in batch_hashes:
            return _DedupOutcome(added_membership=False)
        existing = self.nodes.find_raw_by_hash(trunc_hash)
        if existing is not None:
            # Same content already known. Add to this workspace if not present.
            added = False
            existing_members = self.conn.execute(
                "SELECT 1 FROM workspace_membership WHERE workspace_id = ? AND node_id = ?",
                (self.workspace_id, existing.id),
            ).fetchone()
            if existing_members is None:
                self.workspaces.add_member(self.workspace_id, existing.id)
                added = True
            return _DedupOutcome(added_membership=added)
        extracted = extract(path)
        if extracted is None:
            return None
        try:
            raw_bytes = path.read_bytes()
        except OSError as exc:
            log.warning("read_bytes failed for %s: %s", path, exc)
            return None
        chunks = chunk_doc(extracted.text, extracted.subkind)
        if not chunks:
            log.debug("chunker emitted 0 chunks for %s; skipping", path)
            return None
        if batch_hashes is not None:
            batch_hashes.add(trunc_hash)
        return _Pending(
            path=path, full_hash=full_hash, trunc_hash=trunc_hash,
            size=size, raw_bytes=raw_bytes, extracted=extracted,
            chunks=chunks,
        )

    def _flush_batch(self, batch: list[_Pending], result: IngestResult) -> None:
        """Embed every chunk in the batch in one model call, then write each raw + chunks.

        We collect all chunks across every file in the batch, embed them in a
        single `encode_batch()` call (the model's batched forward pass is far
        cheaper per chunk than one call per file), then split the vector
        matrix back per-file when writing.
        """
        # Flat list of every chunk text, with a per-file slice so we can split
        # the resulting embedding matrix back.
        all_texts: list[str] = []
        slices: list[tuple[int, int]] = []  # (start, end) into all_texts per file
        for p in batch:
            start = len(all_texts)
            all_texts.extend(self._embed_text_for_chunk(p, c) for c in p.chunks)
            slices.append((start, len(all_texts)))

        if not all_texts:
            return
        try:
            vectors = self.embedder.encode_batch(all_texts)
        except Exception as exc:  # noqa: BLE001
            log.exception("batch embedding failed")
            result.errors.append(
                f"embed batch ({len(batch)} files, {len(all_texts)} chunks): {exc}",
            )
            vectors = None

        for pending, (s, e) in zip(batch, slices, strict=True):
            chunk_vecs = vectors[s:e] if vectors is not None else None
            try:
                self._write_one(pending, chunk_vecs)
                result.new_raw += 1
                result.members_added += 1
            except Exception as exc:  # noqa: BLE001
                log.exception("ingest write failed for %s", pending.path)
                result.errors.append(f"{pending.path}: {exc}")

    def _write_one(self, p: _Pending, chunk_vecs: np.ndarray | None) -> None:
        store_blob(p.full_hash, p.raw_bytes)
        title = self._derive_title(p)
        node = RawNode(
            subkind=p.extracted.subkind,
            title=title,
            body=p.extracted.text,
            content_hash=p.trunc_hash,
            canonical_path=str(p.path),
            mime=p.extracted.mime,
            size_bytes=p.size,
        )
        self.nodes.create_raw(node, chunks=p.chunks, chunk_embeddings=chunk_vecs)
        self.workspaces.add_member(self.workspace_id, node.id)

    @staticmethod
    def _embed_text_for_chunk(p: _Pending, chunk: Chunk) -> str:
        """Build the embedding input for a chunk.

        We prepend the file stem and the section heading (if any) so the
        embedding captures "this chunk lives inside X" — useful when two
        files share the same boilerplate and the file identity is the
        disambiguating signal.
        """
        prefix_parts = [p.path.stem]
        if chunk.section:
            prefix_parts.append(chunk.section)
        prefix = " :: ".join(prefix_parts) + "\n\n"
        return (prefix + chunk.text)[:8192]

    @staticmethod
    def _derive_title(p: _Pending) -> str:
        if p.extracted.subkind in {"md", "txt", "transcript"}:
            for line in p.extracted.text.splitlines():
                line = line.strip().lstrip("# ").strip()
                if line:
                    return line[:200]
        return p.path.stem


@dataclass
class _DedupOutcome:
    added_membership: bool


def scan_path(
    conn: sqlite3.Connection,
    workspace_id: str,
    root: Path,
    *,
    embedder: Embedder | None = None,
) -> IngestResult:
    """Convenience wrapper: build a workspace pipeline and run it once."""
    return IngestPipeline(conn, workspace_id, embedder=embedder).scan(root)


def scan_workspace(
    conn: sqlite3.Connection,
    workspace_id: str,
    *,
    embedder: Embedder | None = None,
) -> IngestResult:
    """Walk every root registered for the workspace; return a combined IngestResult.

    The pipeline is reused across roots so the embedder warm-up cost is paid
    once. Each root is independently mark_scanned'd so the user can see which
    roots were last touched.
    """
    ws_repo = WorkspaceRepository(conn)
    pipeline = IngestPipeline(conn, workspace_id, embedder=embedder)
    combined = IngestResult()
    for src in ws_repo.list_sources(workspace_id):
        root_path = Path(src.root_path)
        if not root_path.exists():
            combined.errors.append(f"missing source root: {src.root_path}")
            continue
        partial = pipeline.scan(root_path)
        combined.scanned += partial.scanned
        combined.new_raw += partial.new_raw
        combined.deduped += partial.deduped
        combined.skipped += partial.skipped
        combined.members_added += partial.members_added
        combined.errors.extend(partial.errors)
        ws_repo.mark_source_scanned(src.id)
    ws_repo.mark_scanned(workspace_id)
    return combined


def scan_project(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    embedder: Embedder | None = None,
) -> IngestResult:
    """Scan all sources for all non-excluded workspaces linked to a project.

    Convenience wrapper used by the CLI `loci scan <project>` command. For
    per-workspace scanning use scan_workspace() directly.
    """
    ws_repo = WorkspaceRepository(conn)
    combined = IngestResult()
    for workspace, link in ws_repo.linked_workspaces(project_id):
        if link.role == "excluded":
            continue
        partial = scan_workspace(conn, workspace.id, embedder=embedder)
        combined.scanned += partial.scanned
        combined.new_raw += partial.new_raw
        combined.deduped += partial.deduped
        combined.skipped += partial.skipped
        combined.members_added += partial.members_added
        combined.errors.extend(partial.errors)
    return combined

