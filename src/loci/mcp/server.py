"""FastMCP server — new 6-tool surface for loci (concept-graph edition).

The interpretation / DAG layer has been dropped. Resources are raw nodes tagged
with aspects and organised into folders. The six public tools are:

    loci_save      — ingest a URL, file, or text snippet
    loci_recall    — concept-graph-driven retrieval
    loci_aspects   — list / edit aspect labels on a resource or the project vocab
    loci_browse    — tabular browse with folder / aspect / keyword filters
    loci_context   — project summary (counts, folders, top aspects)
    loci_research  — stub (paper research coming in v1.1)

Three MCP resource templates expose @-mention deep-links:

    loci:source://{resource_id}  — full text of a resource
    loci:folder://{folder_path}  — resource list for a folder
    loci:aspect://{label}        — resources tagged with an aspect

Project auto-resolution is unchanged — see loci.mcp.resolve.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from loci.config import get_settings
from loci.db import init_schema
from loci.db.connection import get_connection
from loci.graph.aspects import AspectRepository
from loci.graph.models import new_id, now_iso
from loci.graph.projects import ProjectRepository
from loci.graph.sources import SourceRepository
from loci.graph.workspaces import WorkspaceRepository
from loci.mcp.resolve import ProjectNotFound, resolve_project_id

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Usage logging helper
# ---------------------------------------------------------------------------


async def _log_usage(
    resource_id: str,
    tool_call_type: str,
    conn,
    session_hash: str | None = None,
) -> None:
    """Insert a row into resource_usage_log. Best-effort — never raises."""
    try:
        conn.execute(
            """
            INSERT INTO resource_usage_log
                (id, resource_id, session_hash, tool_call_type, used_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (new_id(), resource_id, session_hash, tool_call_type, now_iso()),
        )
        conn.commit()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def build_mcp_server() -> FastMCP:
    """Construct and return a FastMCP server with all loci tools and resources."""
    settings = get_settings()
    settings.ensure_dirs()
    init_schema()

    mcp = FastMCP(
        name="loci",
        instructions=(
            "loci is a personal knowledge store. Resources (URLs, files, text) are "
            "saved with folder labels and aspect tags. Use `loci_save` to capture "
            "new resources, `loci_recall` to search, `loci_browse` to explore by "
            "folder or aspect, and `loci_aspects` to view or edit tags. "
            "Reference a specific resource with @loci:source://{id}. "
            "Call `loci_context` at the start of a session to see what is available."
        ),
    )

    # -----------------------------------------------------------------------
    # Tool 1: loci_save
    # -----------------------------------------------------------------------

    @mcp.tool(
        name="loci_save",
        description=(
            "Save a resource (URL, local file path, or plain-text snippet) into loci. "
            "If folder or aspects are omitted, loci will suggest them and — when the "
            "client supports it — show an interactive form for confirmation. "
            "Returns a markdown summary of what was saved."
        ),
    )
    async def loci_save(
        url_or_path: str,
        ctx: Context,
        context_text: str | None = None,
        folder: str | None = None,
        aspects: list[str] | None = None,
        project: str | None = None,
    ) -> str:
        from loci.capture.ingest import ingest_file, ingest_text, ingest_url

        conn = get_connection()
        try:
            project_id = resolve_project_id(conn, project)
        except ProjectNotFound as e:
            return f"Error: {e}"

        # --- Detect input type and ingest ---
        import re
        from pathlib import Path

        input_str = url_or_path.strip()
        is_url = bool(re.match(r"^https?://", input_str))
        is_path = not is_url and Path(input_str).exists()

        try:
            if is_url:
                result = await ingest_url(input_str, context_text, project_id, conn)
            elif is_path:
                result = await ingest_file(Path(input_str), context_text, project_id, conn)
            else:
                # Treat as raw text snippet; use first ~60 chars as title.
                title = input_str[:60].split("\n")[0].strip() or "Untitled snippet"
                result = await ingest_text(input_str, title, context_text, project_id, conn)
        except Exception as exc:  # noqa: BLE001
            log.exception("loci_save: ingest failed for %r", input_str)
            return f"Error during ingest: {exc}"

        # --- Resolve folder and aspects ---
        if result.is_duplicate:
            confirmed_folder = folder or result.existing_folder
            confirmed_aspects = aspects if aspects is not None else result.existing_aspects
        else:
            if folder is None or aspects is None:
                # Try elicitation when either value is missing.
                confirmed_folder, confirmed_aspects = await _elicit_folder_and_aspects(
                    ctx=ctx,
                    result=result,
                    caller_folder=folder,
                    caller_aspects=aspects,
                )
            else:
                confirmed_folder = folder
                confirmed_aspects = aspects

        # --- Write provenance folder update ---
        if confirmed_folder:
            conn.execute(
                "UPDATE resource_provenance SET folder = ? WHERE resource_id = ?",
                (confirmed_folder, result.resource_id),
            )
            conn.commit()

        # --- Write aspect tags ---
        if confirmed_aspects:
            aspect_repo = AspectRepository(conn)
            aspect_repo.tag_resource(result.resource_id, confirmed_aspects, source="user")
            conn.commit()

        # --- Add to project membership ---
        proj_repo = ProjectRepository(conn)
        proj_repo.add_member(project_id, result.resource_id, role="included")
        conn.commit()

        # --- Count chunks ---
        chunk_count_row = conn.execute(
            "SELECT COUNT(*) FROM raw_chunks WHERE node_id = ?",
            (result.resource_id,),
        ).fetchone()
        chunk_count = chunk_count_row[0] if chunk_count_row else 0

        dup_note = " (already existed)" if result.is_duplicate else ""
        aspects_str = ", ".join(confirmed_aspects) if confirmed_aspects else "(none)"
        folder_str = confirmed_folder or "(none)"
        return (
            f"Saved: {result.title}{dup_note}\n"
            f"ID: {result.resource_id}\n"
            f"Folder: {folder_str}\n"
            f"Aspects: {aspects_str}\n"
            f"Chunks: {chunk_count}"
        )

    # -----------------------------------------------------------------------
    # Tool 2: loci_recall
    # -----------------------------------------------------------------------

    @mcp.tool(
        name="loci_recall",
        description=(
            "Retrieve resources relevant to a query using concept-graph-driven "
            "retrieval (BM25 + ANN + aspect expansion + graph reranking). "
            "Returns ranked sources with the reason each was surfaced."
        ),
    )
    async def loci_recall(
        query: str,
        n: int = 5,
        filter_aspects: list[str] | None = None,
        filter_folder: str | None = None,
        project: str | None = None,
    ) -> str:
        from loci.retrieve.pipeline import retrieve

        conn = get_connection()
        try:
            project_id = resolve_project_id(conn, project)
        except ProjectNotFound as e:
            return f"Error: {e}"

        try:
            results = await retrieve(
                query=query,
                project_id=project_id,
                conn=conn,
                n=n,
                filter_aspects=filter_aspects,
                filter_folder=filter_folder,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("loci_recall: retrieve failed")
            return f"Error during retrieval: {exc}"

        if not results:
            return f'## Recall: "{query}"\n\nNo results found.'

        # Log usage for each returned resource.
        for r in results:
            await _log_usage(r.resource_id, "loci_recall", conn)

        lines: list[str] = [f'## Recall: "{query}"\n']
        for i, r in enumerate(results, start=1):
            folder_tag = f" [{r.folder}]" if r.folder else ""
            aspects_str = ", ".join(r.aspects) if r.aspects else "—"
            top_text = ""
            if r.chunks:
                raw_text = r.chunks[0].text[:300]
                top_text = raw_text.replace("\n", " ").strip()
                if len(r.chunks[0].text) > 300:
                    top_text += "..."

            lines.append(f"### {i}. {r.title}{folder_tag}")
            lines.append(f"**ID**: `{r.resource_id}`")
            lines.append(f"**Aspects**: {aspects_str}")
            lines.append(f"**Why surfaced**: {r.why_surfaced}")
            if top_text:
                lines.append(f"\n> {top_text}")
            lines.append("")

        return "\n".join(lines)

    # -----------------------------------------------------------------------
    # Tool 3: loci_aspects
    # -----------------------------------------------------------------------

    @mcp.tool(
        name="loci_aspects",
        description=(
            "List or edit aspect labels for a resource or the whole project vocabulary.\n\n"
            'action="list": show current aspects for the resource, or the full vocab if no resource_id.\n'
            'action="add": add aspect labels to a resource (requires resource_id + labels).\n'
            'action="remove": remove aspect labels from a resource (requires resource_id + labels).\n'
            'action="edit": open an interactive form to edit aspects (elicitation, requires resource_id).'
        ),
    )
    async def loci_aspects(
        ctx: Context,
        resource_id: str | None = None,
        action: str = "list",
        labels: list[str] | None = None,
        project: str | None = None,
    ) -> str:
        conn = get_connection()
        try:
            project_id = resolve_project_id(conn, project)
        except ProjectNotFound as e:
            return f"Error: {e}"

        aspect_repo = AspectRepository(conn)

        if action == "list":
            if resource_id is None:
                # Return the full project vocabulary with usage counts.
                top = aspect_repo.top_aspects(project_id, limit=100)
                if not top:
                    return "No aspects in this project yet."
                lines = ["## Project Aspect Vocabulary\n"]
                for label, count in top:
                    lines.append(
                        f"- **{label}** ({count} resource{'s' if count != 1 else ''})"
                    )
                return "\n".join(lines)
            else:
                # Return aspects for the specific resource.
                resource_aspects = aspect_repo.aspects_for(resource_id)
                if not resource_aspects:
                    return f"No aspects for resource `{resource_id}`."
                lines = [f"## Aspects for `{resource_id}`\n"]
                for ra in resource_aspects:
                    label_row = conn.execute(
                        "SELECT label FROM aspect_vocab WHERE id = ?", (ra.aspect_id,)
                    ).fetchone()
                    label = label_row["label"] if label_row else ra.aspect_id
                    lines.append(
                        f"- **{label}** (confidence: {ra.confidence:.2f}, source: {ra.source})"
                    )
                return "\n".join(lines)

        elif action == "add":
            if resource_id is None:
                return "Error: resource_id is required for action='add'."
            if not labels:
                return "Error: labels is required for action='add'."
            aspect_repo.tag_resource(resource_id, labels, source="user")
            conn.commit()
            return f"Added aspects to `{resource_id}`: {', '.join(labels)}"

        elif action == "remove":
            if resource_id is None:
                return "Error: resource_id is required for action='remove'."
            if not labels:
                return "Error: labels is required for action='remove'."
            aspect_repo.untag_resource(resource_id, labels)
            conn.commit()
            return f"Removed aspects from `{resource_id}`: {', '.join(labels)}"

        elif action == "edit":
            if resource_id is None:
                return "Error: resource_id is required for action='edit'."

            # Get current aspects.
            current_aspects = aspect_repo.aspects_for(resource_id)
            current_labels: list[str] = []
            for ra in current_aspects:
                label_row = conn.execute(
                    "SELECT label FROM aspect_vocab WHERE id = ?", (ra.aspect_id,)
                ).fetchone()
                if label_row:
                    current_labels.append(label_row["label"])

            chosen_labels = current_labels[:]

            # Try elicitation with a simple text field (comma-separated).
            try:
                from pydantic import BaseModel

                class AspectEditForm(BaseModel):
                    aspects_csv: str = ", ".join(current_labels)
                    additional: str = ""

                elicit_result = await ctx.elicit(
                    message=(
                        f"Edit aspects for `{resource_id}`.\n"
                        f"Current: {', '.join(current_labels) or '(none)'}"
                    ),
                    schema=AspectEditForm,
                )
                if (
                    elicit_result.action == "accept"
                    and elicit_result.data is not None
                ):
                    data = elicit_result.data
                    csv = (data.aspects_csv or "").strip()
                    extras = (data.additional or "").strip()
                    chosen_labels = [
                        lbl.strip() for lbl in csv.split(",") if lbl.strip()
                    ]
                    if extras:
                        for lbl in extras.split(","):
                            lbl = lbl.strip()
                            if lbl and lbl not in chosen_labels:
                                chosen_labels.append(lbl)
            except Exception:  # noqa: BLE001
                # Elicitation not supported — keep current labels.
                pass

            # Replace all aspects.
            aspect_repo.clear_resource_aspects(resource_id)
            if chosen_labels:
                aspect_repo.tag_resource(resource_id, chosen_labels, source="user")
            conn.commit()
            return (
                f"Updated aspects for `{resource_id}`:\n"
                + (", ".join(chosen_labels) if chosen_labels else "(none)")
            )

        else:
            return (
                f"Error: unknown action {action!r}. "
                "Use 'list', 'add', 'remove', or 'edit'."
            )

    # -----------------------------------------------------------------------
    # Tool 4: loci_browse
    # -----------------------------------------------------------------------

    @mcp.tool(
        name="loci_browse",
        description=(
            "Browse saved resources, optionally filtered by folder, aspect label, "
            "or keyword (title/body substring). Returns a markdown table of matching "
            "resources with Title, ID, Folder, Aspects, and Saved date."
        ),
    )
    async def loci_browse(
        folder: str | None = None,
        aspect: str | None = None,
        query: str | None = None,
        limit: int = 20,
        project: str | None = None,
    ) -> str:
        conn = get_connection()
        try:
            project_id = resolve_project_id(conn, project)
        except ProjectNotFound as e:
            return f"Error: {e}"

        # Build query with optional aspect subquery filter.
        where_parts = ["pm.project_id = ?"]
        params: list[Any] = [project_id]

        if aspect:
            where_parts.append(
                """
                n.id IN (
                    SELECT ra2.resource_id
                    FROM resource_aspects ra2
                    JOIN aspect_vocab av2 ON av2.id = ra2.aspect_id
                    WHERE av2.label = ?
                )
                """
            )
            params.append(aspect)

        if folder:
            where_parts.append("(rp.folder = ? OR rp.folder LIKE ?)")
            params.extend([folder, f"{folder}/%"])

        if query:
            where_parts.append("(n.title LIKE ? OR n.body LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])

        sql = f"""
            SELECT
                n.id,
                n.title,
                n.created_at,
                rp.folder,
                GROUP_CONCAT(av.label, ', ') AS aspects
            FROM nodes n
            JOIN project_effective_members pm ON pm.node_id = n.id
            LEFT JOIN resource_provenance rp ON rp.resource_id = n.id
            LEFT JOIN resource_aspects ra ON ra.resource_id = n.id
            LEFT JOIN aspect_vocab av ON av.id = ra.aspect_id
            WHERE {" AND ".join(where_parts)}
            GROUP BY n.id
            ORDER BY n.created_at DESC
            LIMIT ?
        """
        params.append(limit)

        try:
            rows = conn.execute(sql, params).fetchall()
        except Exception as exc:  # noqa: BLE001
            log.exception("loci_browse: SQL failed")
            return f"Error during browse: {exc}"

        if not rows:
            filters = []
            if folder:
                filters.append(f"folder={folder!r}")
            if aspect:
                filters.append(f"aspect={aspect!r}")
            if query:
                filters.append(f"keyword={query!r}")
            filter_str = ", ".join(filters) or "no filters"
            return f"No resources found ({filter_str})."

        lines = ["## Resources\n"]
        lines.append("| Title | ID | Folder | Aspects | Saved |")
        lines.append("|-------|-----|--------|---------|-------|")
        for row in rows:
            title = (row["title"] or "Untitled")[:50]
            rid = row["id"]
            folder_cell = row["folder"] or "—"
            aspects_cell = row["aspects"] or "—"
            if len(aspects_cell) > 40:
                aspects_cell = aspects_cell[:37] + "..."
            saved = (row["created_at"] or "")[:10]
            lines.append(
                f"| {title} | `{rid}` | {folder_cell} | {aspects_cell} | {saved} |"
            )

        return "\n".join(lines)

    # -----------------------------------------------------------------------
    # Tool 5: loci_context
    # -----------------------------------------------------------------------

    @mcp.tool(
        name="loci_context",
        description=(
            "Return a summary of the current loci project: resource count, folder "
            "structure, top aspects, and workspace links. Call at the start of a "
            "session to understand what knowledge is available."
        ),
    )
    async def loci_context(project: str | None = None) -> str:
        conn = get_connection()
        try:
            project_id = resolve_project_id(conn, project)
        except ProjectNotFound as e:
            return f"Error: {e}"

        proj = ProjectRepository(conn).get(project_id)
        if proj is None:
            return "Error: project not found."

        # Resource count.
        count_row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM project_effective_members pm
            JOIN nodes n ON n.id = pm.node_id
            WHERE pm.project_id = ? AND n.kind = 'raw'
            """,
            (project_id,),
        ).fetchone()
        resource_count = count_row["cnt"] if count_row else 0

        # Folder tree.
        folder_rows = conn.execute(
            """
            SELECT rp.folder, COUNT(*) AS cnt
            FROM resource_provenance rp
            JOIN project_effective_members pm ON pm.node_id = rp.resource_id
            WHERE pm.project_id = ?
              AND rp.folder IS NOT NULL
              AND rp.folder != ''
            GROUP BY rp.folder
            ORDER BY cnt DESC
            """,
            (project_id,),
        ).fetchall()

        # Top 10 aspects.
        aspect_repo = AspectRepository(conn)
        top_aspects = aspect_repo.top_aspects(project_id, limit=10)

        # Workspaces.
        ws_repo = WorkspaceRepository(conn)
        ws_links = ws_repo.linked_workspaces(project_id)

        lines: list[str] = []
        lines.append(f"## loci project: {proj.name or proj.slug}")
        lines.append(f"**ID**: `{proj.id}`  **Slug**: `{proj.slug}`")
        lines.append(f"**Resources**: {resource_count}")
        lines.append("")

        if folder_rows:
            lines.append("### Folders")
            for row in folder_rows:
                cnt = row["cnt"]
                lines.append(
                    f"- `{row['folder']}` ({cnt} resource{'s' if cnt != 1 else ''})"
                )
            lines.append("")

        if top_aspects:
            lines.append("### Top aspects")
            for label, cnt in top_aspects:
                lines.append(f"- **{label}** ({cnt})")
            lines.append("")

        if ws_links:
            lines.append("### Workspaces")
            for ws, link in ws_links:
                if link.role == "excluded":
                    continue
                lines.append(f"- **{ws.name or ws.slug}** (role: {link.role})")
            lines.append("")

        lines.append("### Usage hints")
        lines.append("- `loci_recall` — semantic search over resources")
        lines.append("- `loci_save` — capture a URL, file, or text snippet")
        lines.append("- `loci_browse` — filter by folder or aspect")
        lines.append(
            "- `@loci:source://{id}` — reference a specific resource in context"
        )

        return "\n".join(lines)

    # -----------------------------------------------------------------------
    # Tool 6: loci_research (stub — v1.1)
    # -----------------------------------------------------------------------

    @mcp.tool(
        name="loci_research",
        description=(
            "Start a paper-discovery research run. "
            "(Paper research is coming in loci v1.1 — this tool is a stub.)"
        ),
    )
    async def loci_research(
        query: str,
        project: str | None = None,
    ) -> str:
        return (
            "Paper research is coming in loci v1.1. "
            "In the meantime, use `loci_save` to capture a URL or PDF "
            "and `loci_recall` to search your existing library."
        )

    # -----------------------------------------------------------------------
    # Workspace list (kept for compatibility)
    # -----------------------------------------------------------------------

    @mcp.tool(
        name="loci_workspace_list",
        description="List all information workspaces.",
    )
    async def loci_workspace_list() -> str:
        conn = get_connection()
        workspaces = WorkspaceRepository(conn).list()
        if not workspaces:
            return "No workspaces found."
        lines = ["## Workspaces\n"]
        lines.append("| Slug | Name | Kind |")
        lines.append("|------|------|------|")
        for ws in workspaces:
            lines.append(f"| {ws.slug} | {ws.name} | {ws.kind} |")
        return "\n".join(lines)

    # -----------------------------------------------------------------------
    # MCP Resources — @-mention deep links
    # -----------------------------------------------------------------------

    @mcp.resource("loci:source://{resource_id}")
    async def get_source(resource_id: str) -> str:
        """Full text of a resource by ID."""
        conn = get_connection()
        src_repo = SourceRepository(conn)
        node = src_repo.get(resource_id)
        if node is None:
            return f"Resource not found: {resource_id}"

        await _log_usage(resource_id, "resource_get_source", conn)

        # Fetch provenance.
        prov_row = conn.execute(
            """
            SELECT folder, source_url, context_text
            FROM resource_provenance
            WHERE resource_id = ?
            """,
            (resource_id,),
        ).fetchone()
        folder = prov_row["folder"] if prov_row else None
        source_url = prov_row["source_url"] if prov_row else None

        # Fetch aspects.
        aspect_repo = AspectRepository(conn)
        resource_aspects = aspect_repo.aspects_for(resource_id)
        aspect_labels: list[str] = []
        for ra in resource_aspects:
            label_row = conn.execute(
                "SELECT label FROM aspect_vocab WHERE id = ?", (ra.aspect_id,)
            ).fetchone()
            if label_row:
                aspect_labels.append(label_row["label"])

        header_parts = [f"# {node.title}"]
        if folder:
            header_parts.append(f"**Folder**: {folder}")
        if source_url:
            header_parts.append(f"**Source**: {source_url}")
        if aspect_labels:
            header_parts.append(f"**Aspects**: {', '.join(aspect_labels)}")
        header_parts.append(f"**ID**: `{resource_id}`")
        header_parts.append("")
        header_parts.append(node.body or "(no content)")

        return "\n".join(header_parts)

    @mcp.resource("loci:folder://{folder_path}")
    async def get_folder(folder_path: str) -> str:
        """List of resources in a folder."""
        conn = get_connection()

        try:
            project_id = resolve_project_id(conn)
        except ProjectNotFound:
            project_id = None

        if project_id:
            rows = conn.execute(
                """
                SELECT n.id, n.title, n.created_at
                FROM nodes n
                JOIN project_effective_members pm ON pm.node_id = n.id
                JOIN resource_provenance rp ON rp.resource_id = n.id
                WHERE pm.project_id = ?
                  AND (rp.folder = ? OR rp.folder LIKE ?)
                ORDER BY n.created_at DESC
                LIMIT 50
                """,
                (project_id, folder_path, f"{folder_path}/%"),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT n.id, n.title, n.created_at
                FROM nodes n
                JOIN resource_provenance rp ON rp.resource_id = n.id
                WHERE rp.folder = ? OR rp.folder LIKE ?
                ORDER BY n.created_at DESC
                LIMIT 50
                """,
                (folder_path, f"{folder_path}/%"),
            ).fetchall()

        if not rows:
            return f"No resources in folder: {folder_path}"

        lines = [f"# Folder: {folder_path}\n"]
        for row in rows:
            saved = (row["created_at"] or "")[:10]
            title = row["title"] or "Untitled"
            lines.append(
                f"- [{title}](@loci:source://{row['id']}) — {saved}"
            )

        return "\n".join(lines)

    @mcp.resource("loci:aspect://{label}")
    async def get_aspect_resources(label: str) -> str:
        """Resources tagged with this aspect."""
        conn = get_connection()

        try:
            project_id = resolve_project_id(conn)
        except ProjectNotFound:
            project_id = None

        aspect_repo = AspectRepository(conn)
        resource_ids = aspect_repo.resources_for_aspect(
            label, project_id=project_id, limit=50
        )

        if not resource_ids:
            return f"No resources tagged with aspect: {label}"

        src_repo = SourceRepository(conn)
        nodes = src_repo.get_many(resource_ids)

        lines = [f"# Aspect: {label}\n"]
        for node in nodes:
            prov_row = conn.execute(
                "SELECT folder FROM resource_provenance WHERE resource_id = ?",
                (node.id,),
            ).fetchone()
            folder = prov_row["folder"] if prov_row else None
            folder_tag = f" [{folder}]" if folder else ""
            title = node.title or "Untitled"
            lines.append(
                f"- [{title}](@loci:source://{node.id}){folder_tag}"
            )

        return "\n".join(lines)

    return mcp


# ---------------------------------------------------------------------------
# Elicitation helper
# ---------------------------------------------------------------------------


async def _elicit_folder_and_aspects(
    ctx: Context,
    result: Any,
    caller_folder: str | None,
    caller_aspects: list[str] | None,
) -> tuple[str | None, list[str]]:
    """Try to elicit folder and aspect choices from the user via MCP elicitation.

    Falls back gracefully to top suggestions when elicitation is not supported
    or when the user declines.

    Args:
        ctx: The FastMCP Context object for this tool call.
        result: A CaptureResult with folder_suggestions and aspect_suggestions.
        caller_folder: Folder value passed by the caller (may be None).
        caller_aspects: Aspects list passed by the caller (may be None).

    Returns:
        (confirmed_folder, confirmed_aspects) tuple.
    """
    # Determine suggestion-based defaults.
    top_folder = caller_folder
    if top_folder is None and result.folder_suggestions:
        top_folder = result.folder_suggestions[0][0]

    top_aspects: list[str] = []
    if caller_aspects is not None:
        top_aspects = list(caller_aspects)
    elif result.aspect_suggestions:
        top_aspects = result.aspect_suggestions[:3]

    try:
        from pydantic import BaseModel, Field

        # Determine default folder string.
        folder_default = top_folder or ""
        folder_options_note = ""
        if result.folder_suggestions:
            options = [f[0] for f in result.folder_suggestions]
            folder_options_note = f" (suggestions: {', '.join(options)})"

        aspects_default = ", ".join(top_aspects)

        class SaveConfirmForm(BaseModel):
            folder: str = Field(
                default=folder_default,
                description=f"Folder label for this resource{folder_options_note}",
            )
            aspects: str = Field(
                default=aspects_default,
                description="Comma-separated aspect labels for this resource",
            )

        elicit_result = await ctx.elicit(
            message=f"Confirm folder and aspects for: {result.title!r}",
            schema=SaveConfirmForm,
        )

        if (
            elicit_result.action == "accept"
            and elicit_result.data is not None
        ):
            data = elicit_result.data
            chosen_folder = (data.folder or "").strip() or top_folder
            aspects_raw = (data.aspects or "").strip()
            chosen_aspects = [
                lbl.strip() for lbl in aspects_raw.split(",") if lbl.strip()
            ]
            return chosen_folder, chosen_aspects

    except Exception:  # noqa: BLE001
        # Elicitation not supported or failed — fall through to defaults.
        pass

    return top_folder, top_aspects


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_stdio() -> None:
    """Run MCP over stdio (for Claude Code, which subprocesses MCP servers)."""
    server = build_mcp_server()
    server.run(transport="stdio")
