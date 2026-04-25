"""Content-addressed hashing + blob storage.

PLAN.md §Storage commits to "Raw blobs on disk, content-addressed". We use
sha256 of the file bytes; the full hex digest is the blob path. We store a
truncated form (16 hex chars = 64 bits) in `raw_nodes.content_hash` for
indexing, but the on-disk blob path uses the full digest so collisions are
detectable later.

Blob layout:

    <blob_dir>/<hash[:2]>/<hash[2:]>

The two-level fan-out keeps directory listings tractable. With 100k blobs
spread evenly across 256 first-level buckets, each bucket has ~390 entries —
well within macOS HFS+/APFS comfortable range.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from loci.config import get_settings

# How many hex characters from the full sha256 digest go into raw_nodes.content_hash.
# 16 hex = 64 bits. PLAN.md uses [:16] in the schema example.
HASH_TRUNC = 16


def hash_file(path: Path) -> tuple[str, str, int]:
    """Hash a file, returning (full_digest, truncated, size_bytes).

    Reads in 1 MB chunks so we don't blow up RAM on large PDFs.
    """
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    full = h.hexdigest()
    return full, full[:HASH_TRUNC], size


def hash_bytes(data: bytes) -> tuple[str, str]:
    """Hash a bytes object. Returns (full_digest, truncated)."""
    h = hashlib.sha256(data).hexdigest()
    return h, h[:HASH_TRUNC]


def blob_path(full_hash: str) -> Path:
    """Return the on-disk path for a blob with the given full sha256 digest."""
    settings = get_settings()
    return settings.blob_dir / full_hash[:2] / full_hash[2:]


def store_blob(full_hash: str, data: bytes) -> Path:
    """Write blob bytes to disk. Idempotent (no-op if path exists)."""
    target = blob_path(full_hash)
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    # Atomic-ish write: temp then rename. Avoids half-written blobs on crash.
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(target)
    return target


def read_blob(full_hash: str) -> bytes | None:
    p = blob_path(full_hash)
    if not p.exists():
        return None
    return p.read_bytes()
