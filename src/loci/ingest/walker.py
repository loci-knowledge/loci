"""Filesystem walker.

Yield candidate files for ingest. We support two input modes:

1. A directory — recursive walk with `os.walk`-style traversal, filtered by
   a default ignore list (dot-dirs, common build/cache dirs, binaries we
   can't extract).
2. A glob pattern — `**/*.pdf` etc.

The walker doesn't do any I/O on the file content — it only emits paths.
The pipeline reads/hashes/extracts.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

log = logging.getLogger(__name__)

# Directories we always skip. Personal vaults rarely have meaningful content
# in these and they balloon walk time.
DEFAULT_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    ".next", ".turbo", ".cache", "target",
})

# Extensions we know how to extract. PLAN.md §Memory space lists pdf/md/code/
# html/transcript; we cover those plus a few obvious aliases. Anything not in
# this set is silently skipped — the user can extend in the project config.
DEFAULT_INCLUDE_EXTS: frozenset[str] = frozenset({
    # Text + markdown
    ".md", ".mdx", ".markdown", ".txt", ".rst", ".org",
    # PDFs
    ".pdf",
    # HTML
    ".html", ".htm",
    # Source code (sample — extend per project)
    ".py", ".js", ".ts", ".jsx", ".tsx", ".rs", ".go", ".rb", ".java",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".swift", ".kt", ".scala",
    ".sh", ".bash", ".zsh", ".sql", ".lua", ".r", ".R", ".jl",
    # Config that often contains docstrings / comments worth indexing
    ".toml", ".yaml", ".yml", ".json",
    # Transcripts — plain text under arbitrary extensions; users can pre-rename
    ".vtt", ".srt",
})


def walk(
    root: Path,
    *,
    include_exts: frozenset[str] = DEFAULT_INCLUDE_EXTS,
    skip_dirs: frozenset[str] = DEFAULT_SKIP_DIRS,
    max_size_bytes: int = 50 * 1024 * 1024,
) -> Iterator[Path]:
    """Yield file paths under `root` matching the include/skip filters.

    Files larger than `max_size_bytes` (default 50 MB) are skipped — they're
    almost always binaries or generated, and the embedder doesn't benefit
    from them anyway. Override per project if you really need to ingest a
    huge transcript.
    """
    root = root.expanduser().resolve()
    if root.is_file():
        if _accept(root, include_exts, max_size_bytes):
            yield root
        return
    if not root.is_dir():
        log.warning("walk: %s is neither file nor directory; skipping", root)
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # In-place mutation of dirnames is the documented way to prune `os.walk`.
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            p = Path(dirpath) / name
            if _accept(p, include_exts, max_size_bytes):
                yield p


def _accept(p: Path, include_exts: frozenset[str], max_size_bytes: int) -> bool:
    if p.suffix.lower() not in include_exts:
        return False
    try:
        size = p.stat().st_size
    except OSError:
        return False
    return not (size == 0 or size > max_size_bytes)
