"""Ingest pipeline.

Walk a directory or glob → content-hash each file → dedup against `raw_nodes`
→ extract text → batch-embed → write `RawNode` + `ProjectMembership`. Blob bytes
are stored on disk under `blob_dir/<hash[:2]>/<hash[2:]>` so we can re-extract
later without re-reading the source file.

Public API:
    from loci.ingest import IngestPipeline, scan_path
"""

from loci.ingest.pipeline import (
    IngestPipeline,
    IngestResult,
    scan_path,
    scan_project,
    scan_registered_sources,
    scan_workspace,
)

__all__ = [
    "IngestPipeline",
    "IngestResult",
    "scan_path",
    "scan_project",
    "scan_registered_sources",
    "scan_workspace",
]
