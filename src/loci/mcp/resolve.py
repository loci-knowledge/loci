"""Project auto-resolution for MCP tools.

Resolution order (first match wins):
  1. `project_arg` parameter — slug or id, passed by the caller
  2. `LOCI_PROJECT` environment variable — slug or id
  3. `.loci/project.toml` (or legacy `.loci/project`) walk-up from cwd
  4. `~/.loci/state/current` — pinned slug for MCP sessions

Usage in MCP tools:
    project_id = resolve_project_id(conn, project_arg)

where `project_arg` is the optional `project` parameter passed by the user.
If `project_arg` is given it takes precedence over auto-resolution.
"""

from __future__ import annotations

import os
import sqlite3
import tomllib
from pathlib import Path


class ProjectNotFound(Exception):
    """Raised when no project can be resolved and none was specified."""


def find_project_dir(start: Path | None = None) -> Path | None:
    """Walk up from `start` looking for a `.loci/` directory containing
    `project.toml` or the legacy `project` text file.

    Returns the `.loci/` directory path, or None if not found.
    """
    start = (start or Path.cwd()).resolve()
    current = start
    while True:
        loci_dir = current / ".loci"
        if (loci_dir / "project.toml").is_file() or (loci_dir / "project").is_file():
            return loci_dir
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def find_project_file(start: Path | None = None) -> str | None:
    """Walk up from `start` (default: cwd) looking for `.loci/project.toml`
    or the legacy `.loci/project` text file.

    Returns the slug string, or None if not found.
    """
    start = (start or Path.cwd()).resolve()
    current = start
    while True:
        loci_dir = current / ".loci"
        # Prefer TOML format
        toml_file = loci_dir / "project.toml"
        if toml_file.is_file():
            slug = _read_slug_from_toml(toml_file)
            if slug:
                return slug
        # Legacy text format
        text_file = loci_dir / "project"
        if text_file.is_file():
            slug = text_file.read_text(encoding="utf-8").strip()
            if slug:
                return slug
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _read_slug_from_toml(path: Path) -> str | None:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return data.get("slug") or None
    except Exception:
        return None


def _state_file_path() -> Path:
    from loci.config import get_settings
    return get_settings().state_dir / "current"


def read_state_file() -> str | None:
    """Return the slug from ~/.loci/state/current, or None."""
    p = _state_file_path()
    if p.is_file():
        slug = p.read_text(encoding="utf-8").strip()
        return slug or None
    return None


def write_state_file(slug: str) -> Path:
    """Write `slug` to ~/.loci/state/current for MCP sessions."""
    p = _state_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(slug + "\n", encoding="utf-8")
    return p


def clear_state_file() -> None:
    """Remove ~/.loci/state/current."""
    p = _state_file_path()
    if p.exists():
        p.unlink()


def resolve_project_id(
    conn: sqlite3.Connection,
    project_arg: str | None = None,
    *,
    cwd: Path | None = None,
) -> str:
    """Return the project id to use for a tool call.

    Precedence:
      1. `project_arg` (explicit from caller)
      2. `LOCI_PROJECT` env var
      3. `.loci/project.toml` or `.loci/project` file walk-up from `cwd`
      4. `~/.loci/state/current` file

    Raises `ProjectNotFound` if nothing resolves to a known project.
    """
    slug_or_id = (
        project_arg
        or os.environ.get("LOCI_PROJECT")
        or find_project_file(cwd)
        or read_state_file()
    )
    if slug_or_id is None:
        raise ProjectNotFound(
            "No project specified. Set LOCI_PROJECT, create a .loci/project.toml "
            "file (run `loci project bind <slug>`), run `loci current set <slug>`, "
            "or pass project= explicitly."
        )
    row = conn.execute(
        "SELECT id FROM projects WHERE slug = ? OR id = ?",
        (slug_or_id, slug_or_id),
    ).fetchone()
    if row is None:
        raise ProjectNotFound(f"Project not found: {slug_or_id!r}")
    return row["id"]


_GITIGNORE_CONTENT = """\
# Loci per-repo state — text artifacts are intentionally committable.
# Uncomment the lines below to keep those artifact types out of git:
# views/
# research/
# drafts/
"""


def write_project_toml(slug: str, directory: Path | None = None) -> Path:
    """Write `.loci/project.toml` in `directory` (default: cwd).

    Creates `.loci/` if needed and writes a `.gitignore` alongside.
    """
    directory = (directory or Path.cwd()).resolve()
    loci_dir = directory / ".loci"
    loci_dir.mkdir(exist_ok=True)

    import datetime
    toml_path = loci_dir / "project.toml"
    toml_path.write_text(
        f'slug = "{slug}"\ncreated_at = "{datetime.date.today().isoformat()}"\n',
        encoding="utf-8",
    )

    gitignore_path = loci_dir / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text(_GITIGNORE_CONTENT, encoding="utf-8")

    return toml_path


def write_project_file(slug: str, directory: Path | None = None) -> Path:
    """Write `.loci/project.toml` in `directory` (default: cwd).

    Legacy alias — now writes TOML instead of the old text format.
    """
    return write_project_toml(slug, directory)
