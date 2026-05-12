"""MCP completion handler for loci URI template variables and prompt arguments.

Wired via @mcp.completion() in build_mcp_server(). Provides live autocomplete
suggestions for:
    loci:folder://{folder_path}  → distinct folder labels matching the prefix
    loci:aspect://{label}        → aspect vocab labels matching the prefix
    /loci prompt workspace_slug  → workspace slugs (linked-first) matching prefix
"""

from __future__ import annotations

import random
import sqlite3

from mcp.types import Completion, CompletionArgument, PromptReference, ResourceTemplateReference

from loci.config import get_settings


async def handle_completion(
    ref: object,
    argument: CompletionArgument,
    conn: sqlite3.Connection,
    *,
    invocation_cwd: str | None = None,
) -> Completion | None:
    partial = (argument.value or "").lower()

    # Prompt-argument completions (e.g. /loci <workspace_slug>)
    if isinstance(ref, PromptReference):
        if getattr(ref, "name", None) == "loci" and argument.name == "workspace_slug":
            from loci.graph.workspaces import WorkspaceRepository
            from loci.graph.projects import ProjectRepository

            project_id: str | None = None
            if invocation_cwd:
                proj = ProjectRepository(conn).get_by_cwd(invocation_cwd)
                if proj:
                    project_id = proj.id

            pairs = WorkspaceRepository(conn).list_with_link_flag(project_id)
            values = [
                ws.slug for ws, _ in pairs
                if not partial or ws.slug.startswith(partial)
            ]
            return Completion(values=values, hasMore=False, total=len(values))
        return None

    if not isinstance(ref, ResourceTemplateReference):
        return None

    uri_template = str(ref.uri)
    partial = (argument.value or "").lower()

    if uri_template == "folder://{folder_path}":
        rows = conn.execute(
            """
            SELECT DISTINCT folder FROM resource_provenance
            WHERE folder IS NOT NULL
              AND lower(folder) LIKE ?
            ORDER BY folder
            LIMIT 100
            """,
            (f"{partial}%",),
        ).fetchall()
        values = [r["folder"] for r in rows if r["folder"]]
        return Completion(values=values, hasMore=False, total=len(values))

    if uri_template == "aspect://{label}":
        rows = conn.execute(
            """
            SELECT label FROM aspect_vocab
            WHERE lower(label) LIKE ?
            ORDER BY label
            LIMIT 100
            """,
            (f"{partial}%",),
        ).fetchall()
        values = [r["label"] for r in rows]

        # 1-in-10 sampling for autocomplete signal capture (opt-in only).
        settings = get_settings()
        if settings.capture_autocomplete and random.random() < 0.1:  # noqa: S311
            from loci.mcp.events import record_event
            await record_event(
                conn=conn,
                tool="autocomplete_aspect",
                project_id=None,
                resource_id=None,
                query=argument.value or None,
                session_hash=None,
                enqueue_inference=False,
            )

        return Completion(values=values, hasMore=False, total=len(values))

    return None
