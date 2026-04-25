"""Project auto-resolution for MCP tools.

Resolution order (first match wins):
  1. `LOCI_PROJECT` environment variable — slug or id
  2. `.loci/project` file — walk up from `cwd` like `.git` discovery
  3. Raise `ProjectNotFound` if neither resolves

Usage in MCP tools:
    project_id = resolve_project_id(conn, project_arg)

where `project_arg` is the optional `project` parameter passed by the user.
If `project_arg` is given it takes precedence over auto-resolution.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


class ProjectNotFound(Exception):
    """Raised when no project can be resolved and none was specified."""


def find_project_file(start: Path | None = None) -> str | None:
    """Walk up from `start` (default: cwd) looking for `.loci/project`.

    Returns the slug string from the file, or None if not found.
    """
    start = (start or Path.cwd()).resolve()
    current = start
    while True:
        candidate = current / ".loci" / "project"
        if candidate.is_file():
            slug = candidate.read_text(encoding="utf-8").strip()
            if slug:
                return slug
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def resolve_project_id(
    conn: sqlite3.Connection,
    project_arg: str | None = None,
    *,
    cwd: Path | None = None,
) -> str:
    """Return the project id to use for a tool call.

    Precedence:
      1. `project_arg` (explicit from caller) — treated as slug or id
      2. `LOCI_PROJECT` env var — slug or id
      3. `.loci/project` file walk-up from `cwd`

    Raises `ProjectNotFound` if nothing resolves to a known project.
    """
    slug_or_id = (
        project_arg
        or os.environ.get("LOCI_PROJECT")
        or find_project_file(cwd)
    )
    if slug_or_id is None:
        raise ProjectNotFound(
            "No project specified. Set LOCI_PROJECT, create a .loci/project "
            "file (run `loci project bind <slug>`), or pass project= explicitly."
        )
    row = conn.execute(
        "SELECT id FROM projects WHERE slug = ? OR id = ?",
        (slug_or_id, slug_or_id),
    ).fetchone()
    if row is None:
        raise ProjectNotFound(f"Project not found: {slug_or_id!r}")
    return row["id"]


def write_project_file(slug: str, directory: Path | None = None) -> Path:
    """Write `.loci/project` in `directory` (default: cwd).

    Creates `.loci/` if it doesn't exist. Used by `loci project bind`.
    """
    directory = (directory or Path.cwd()).resolve()
    loci_dir = directory / ".loci"
    loci_dir.mkdir(exist_ok=True)
    project_file = loci_dir / "project"
    project_file.write_text(slug + "\n", encoding="utf-8")
    return project_file
