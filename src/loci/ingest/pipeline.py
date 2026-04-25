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
    """A file that's been hashed + extracted, waiting to be embedded + written."""
    path: Path
    full_hash: str
    trunc_hash: str
    size: int
    raw_bytes: bytes
    extracted: ExtractedDoc


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
        for path in walk(root):
            result.scanned += 1
            try:
                outcome = self._stage_file(path)
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
        if batch:
            self._flush_batch(batch, result)
        self.workspaces.touch(self.workspace_id)
        return result

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    def _stage_file(self, path: Path) -> _Pending | _DedupOutcome | None:
        """Per-file pre-embed work: hash → dedup check → extract → return pending."""
        full_hash, trunc_hash, size = hash_file(path)
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
        return _Pending(
            path=path, full_hash=full_hash, trunc_hash=trunc_hash,
            size=size, raw_bytes=raw_bytes, extracted=extracted,
        )

    def _flush_batch(self, batch: list[_Pending], result: IngestResult) -> None:
        """Embed an entire batch in one model call, then write each row."""
        texts = [self._embed_text(p) for p in batch]
        try:
            vectors = self.embedder.encode_batch(texts)
        except Exception as exc:  # noqa: BLE001
            log.exception("batch embedding failed")
            result.errors.append(f"embed batch ({len(batch)} files): {exc}")
            vectors = np.zeros((len(batch), self.embedder.dim), dtype=np.float32)
            embed_failed = True
        else:
            embed_failed = False
        for pending, vec in zip(batch, vectors, strict=True):
            try:
                self._write_one(pending, vec if not embed_failed else None)
                result.new_raw += 1
                result.members_added += 1
            except Exception as exc:  # noqa: BLE001
                log.exception("ingest write failed for %s", pending.path)
                result.errors.append(f"{pending.path}: {exc}")

    def _write_one(self, p: _Pending, vec: np.ndarray | None) -> None:
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
        self.nodes.create_raw(node, embedding=vec)
        self.workspaces.add_member(self.workspace_id, node.id)

    @staticmethod
    def _embed_text(p: _Pending) -> str:
        text = p.extracted.text
        prefix = f"{p.path.stem}\n\n"
        return (prefix + text)[:8192]

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


# Legacy alias so tests that haven't been updated yet keep working.
# Deprecated: use scan_workspace() or scan_project() instead.
def scan_registered_sources(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    embedder: Embedder | None = None,
) -> IngestResult:
    return scan_project(conn, project_id, embedder=embedder)
