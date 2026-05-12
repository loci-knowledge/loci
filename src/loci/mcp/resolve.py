"""Project resolution for MCP tools.

Resolution order (first match wins):
  1. `project_arg` — explicit slug or id passed by the caller
  2. `projects.cwd` — cwd captured at MCP server startup, stored in DB

Projects are auto-created by `loci_use` the first time a user picks a workspace
from inside Claude Code. No environment variables, no .loci files, no state files.
"""

from __future__ import annotations

import sqlite3


class ProjectNotFound(Exception):
    """Raised when no project is bound to the current cwd."""


def resolve_project_id(
    conn: sqlite3.Connection,
    project_arg: str | None = None,
    *,
    cwd: str | None = None,
) -> str:
    """Return the project id to use for a tool call.

    Precedence:
      1. `project_arg` (explicit from caller — slug or id)
      2. cwd lookup against `projects.cwd`

    Raises `ProjectNotFound` if nothing resolves.
    """
    if project_arg:
        row = conn.execute(
            "SELECT id FROM projects WHERE slug = ? OR id = ?",
            (project_arg, project_arg),
        ).fetchone()
        if row is None:
            raise ProjectNotFound(f"Project not found: {project_arg!r}")
        return row["id"]

    if cwd:
        row = conn.execute(
            "SELECT id FROM projects WHERE cwd = ?", (cwd,)
        ).fetchone()
        if row:
            return row["id"]

    raise ProjectNotFound(
        "No workspace bound to this directory yet. Run /loci to pick one."
    )
