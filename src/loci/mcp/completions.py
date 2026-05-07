"""MCP completion handler for loci URI template variables.

Wired via @mcp.completion() in build_mcp_server(). Provides live autocomplete
suggestions when the user types @loci:folder://pa → ["papers"], etc.

Supported templates:
    loci:folder://{folder_path}  → distinct folder labels matching the prefix
    loci:aspect://{label}        → aspect vocab labels matching the prefix
"""

from __future__ import annotations

import sqlite3

from mcp.types import Completion, CompletionArgument, ResourceTemplateReference


async def handle_completion(
    ref: object,
    argument: CompletionArgument,
    conn: sqlite3.Connection,
) -> Completion | None:
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
        return Completion(values=values, hasMore=False, total=len(values))

    return None
