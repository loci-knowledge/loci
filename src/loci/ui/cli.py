"""loci CLI.

Powered by cyclopts (>=3) for typed CLIs without ceremony.

Subcommands:
    loci config init                      write ~/.loci/.env + config.toml
    loci server [--host] [--port] [--no-worker]
    loci mcp                              MCP stdio server (for Claude Code)
    loci worker [--poll-interval]
    loci project create <slug>            interactive setup wizard
    loci project list
    loci project info <slug>
    loci project bind <slug>              write .loci/project.toml in cwd
    loci current set/clear/show           pin a project for MCP sessions
    loci workspace create/list/info/add-source/link/unlink/scan
    loci scan <project>                   scan all linked workspaces
    loci save <url_or_path>               save a resource (URL or file path)
    loci use [workspace_slugs...]         set active project/workspaces + show table
    loci recall <query>                   retrieve relevant resources
    loci aspects [resource_id]            view or edit aspect labels
    loci doctor                           print resolved storage paths
    loci reset
    loci status [project]
    loci export [project]                 write resource summary
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from cyclopts import App
from rich.console import Console
from rich.table import Table

from loci import __version__
from loci.config import get_settings

# Logging set up once for the CLI. Routes the loci packages at INFO; quieter
# for noisy third-party libs (uvicorn, anthropic).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("anthropic").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

console = Console()

app = App(
    name="loci",
    help="Personal memory graph server.",
    version=__version__,
)
config_app = App(name="config", help="Configuration commands.")
app.command(config_app)
project_app = App(name="project", help="Project commands.")
app.command(project_app)
workspace_app = App(name="workspace", help="Information workspace commands.")
app.command(workspace_app)
current_app = App(name="current", help="Manage the pinned project for MCP sessions.")
app.command(current_app)
event_app = App(name="event", help="Ingest external signals into loci.")
app.command(event_app)
aspect_app = App(name="aspect", help="Aspect provenance and graph commands.")
app.command(aspect_app)


# ---------------------------------------------------------------------------
# Config commands
# ---------------------------------------------------------------------------


@config_app.command(name="init")
def config_init(force: bool = False) -> None:
    """Write ~/.loci/.env (provider keys) and ~/.loci/config.toml (settings).

    Safe to re-run — skips existing files unless --force is passed.
    """
    import stat

    data_dir = Path.home() / ".loci"
    data_dir.mkdir(parents=True, exist_ok=True)

    env_path = data_dir / ".env"
    toml_path = data_dir / "config.toml"

    if env_path.exists() and not force:
        console.print(f"[yellow]skip[/yellow] {env_path} already exists (use --force to overwrite)")
    else:
        env_path.write_text(
            "# loci provider keys\n"
            "# Add at least one of the following:\n"
            "OPENAI_API_KEY=\n"
            "OPENROUTER_API_KEY=\n"
            "ANTHROPIC_API_KEY=\n"
            "\n"
            "# Optional: model overrides (format: <provider>:<model>)\n"
            "# LOCI_RAG_MODEL=openai:openai:gpt-5.4\n"
            "# LOCI_HYDE_MODEL=openai:openai:gpt-5.4-mini\n",
            encoding="utf-8",
        )
        env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0o600
        console.print(f"[green]wrote[/green] {env_path} (chmod 600)")

    if toml_path.exists() and not force:
        console.print(
            f"[yellow]skip[/yellow] {toml_path} already exists (use --force to overwrite)"
        )
    else:
        toml_path.write_text(
            "# loci non-secret settings — uncomment to override defaults\n"
            "\n"
            '# embedding_model = "BAAI/bge-small-en-v1.5"\n'
            '# embedding_device = "auto"  # auto | cpu | mps | cuda\n'
            '# rag_model = "openrouter:anthropic/claude-opus-4.7"\n'
            '# hyde_model = "openrouter:deepseek/deepseek-v4-flash"\n'
            "# port = 7077\n",
            encoding="utf-8",
        )
        console.print(f"[green]wrote[/green] {toml_path}")

    console.print()
    console.print(
        "Next: edit [bold]~/.loci/.env[/bold] and fill in at least one API key, "
        "then run [bold]loci doctor[/bold] to verify."
    )


# ---------------------------------------------------------------------------
# Server / MCP / worker
# ---------------------------------------------------------------------------


@app.command
def server(
    host: str | None = None,
    port: int | None = None,
    no_worker: bool = False,
) -> None:
    """Start the loci HTTP server (and the in-process job worker by default)."""
    import uvicorn

    settings = get_settings()
    settings.ensure_dirs()

    if not no_worker:
        from loci.jobs.worker import start_worker_thread

        start_worker_thread()
        console.print("[dim]worker thread started[/dim]")

    uvicorn.run(
        "loci.api.app:create_app",
        factory=True,
        host=host or settings.host,
        port=port or settings.port,
        log_level="info",
    )


@app.command
def mcp() -> None:
    """Run the loci MCP server over stdio (for Claude Code)."""
    from loci.mcp import run_stdio

    run_stdio()


@app.command
def worker(poll_interval: float = 1.0) -> None:
    """Run the job worker without the HTTP server."""
    from loci.jobs.worker import run_worker_loop

    run_worker_loop(poll_interval=poll_interval)


# ---------------------------------------------------------------------------
# Project commands
# ---------------------------------------------------------------------------


@project_app.command(name="create")
def project_create(
    slug: str,
    name: str | None = None,
    profile: Path | None = None,
    yes: bool = False,
) -> None:
    """Create a project.

    Launches the interactive TUI wizard when stdin is a terminal. Pass
    --yes (or pipe input) to skip the wizard and create non-interactively.
    """
    import sys

    from loci.db import init_schema
    from loci.db.connection import connect

    init_schema()
    conn = connect()

    if sys.stdin.isatty() and not yes:
        from loci.ui.tui import run_wizard

        run_wizard(conn, slug_hint=slug)
    else:
        from loci.graph import Project, ProjectRepository

        profile_md = profile.read_text() if profile else ""
        proj = ProjectRepository(conn).create(
            Project(
                slug=slug,
                name=name or slug,
                profile_md=profile_md,
            )
        )
        conn.commit()
        console.print(f"[green]created[/green] [bold]{proj.slug}[/bold] ({proj.id})")


@project_app.command(name="manage")
def project_manage() -> None:
    """Open the interactive TUI project manager (list, edit, delete, create)."""
    from loci.db import init_schema
    from loci.db.connection import connect
    from loci.ui.tui import run_wizard

    init_schema()
    conn = connect()
    run_wizard(conn)


@project_app.command(name="bind")
def project_bind(slug: str) -> None:
    """Bind the current directory to a project. Writes .loci/project."""
    from loci.mcp.resolve import write_project_file

    path = write_project_file(slug)
    console.print(f"[green]bound[/green] [bold]{slug}[/bold] → {path}")


@project_app.command(name="list")
def project_list() -> None:
    """List all projects."""
    from loci.db import init_schema
    from loci.db.connection import connect
    from loci.graph import ProjectRepository

    init_schema()
    conn = connect()
    table = Table("slug", "name", "id", "last_active_at")
    for p in ProjectRepository(conn).list():
        table.add_row(p.slug, p.name, p.id, p.last_active_at)
    console.print(table)


@project_app.command(name="info")
def project_info(slug: str) -> None:
    """Show details for one project."""
    from loci.db import init_schema
    from loci.db.connection import connect
    from loci.graph import ProjectRepository

    init_schema()
    conn = connect()
    proj = ProjectRepository(conn).get_by_slug(slug)
    if proj is None:
        console.print(f"[red]no such project:[/red] {slug}")
        raise SystemExit(1)
    members = conn.execute(
        "SELECT COUNT(*) AS c FROM project_membership WHERE project_id = ?",
        (proj.id,),
    ).fetchone()["c"]
    console.print(
        {
            "id": proj.id,
            "slug": proj.slug,
            "name": proj.name,
            "members": members,
            "last_active_at": proj.last_active_at,
        }
    )
    if proj.profile_md:
        console.rule("profile")
        console.print(proj.profile_md)


# ---------------------------------------------------------------------------
# Current project (MCP pin)
# ---------------------------------------------------------------------------

_STATE_FILE_NAME = "current"


def _state_file_path() -> Path:
    settings = get_settings()
    state_dir = settings.data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / _STATE_FILE_NAME


@current_app.command(name="set")
def current_set(slug: str) -> None:
    """Pin a project for MCP sessions that lack a .loci/project walk-up."""
    path = _state_file_path()
    path.write_text(slug + "\n", encoding="utf-8")
    console.print(f"[green]set[/green] current project → [bold]{slug}[/bold] ({path})")


@current_app.command(name="clear")
def current_clear() -> None:
    """Clear the pinned MCP project."""
    path = _state_file_path()
    if path.exists():
        path.unlink()
    console.print("[green]cleared[/green] current project")


@current_app.command(name="show")
def current_show() -> None:
    """Show the pinned MCP project (if any)."""
    path = _state_file_path()
    if path.exists():
        slug = path.read_text(encoding="utf-8").strip()
        if slug:
            console.print(f"current project: [bold]{slug}[/bold]")
            return
    console.print("[dim]no current project set[/dim]")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_project(conn, project_str: str):
    from loci.graph import ProjectRepository

    repo = ProjectRepository(conn)
    proj = repo.get_by_slug(project_str) or repo.get(project_str)
    if proj is None:
        console.print(f"[red]no such project:[/red] {project_str}")
        raise SystemExit(1)
    return proj


def _resolve_project_id_auto(conn, project: str | None) -> str:
    """Resolve project id from explicit slug/id or auto-resolution."""
    from loci.mcp.resolve import ProjectNotFound, resolve_project_id

    try:
        return resolve_project_id(conn, project)
    except ProjectNotFound as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc


def _session_toml_path() -> Path | None:
    """Return the path to .loci/session.toml in cwd, creating .loci/ if needed."""
    loci_dir = Path.cwd() / ".loci"
    loci_dir.mkdir(exist_ok=True)
    return loci_dir / "session.toml"


def _resolve_workspace(conn, ws_str: str):
    from loci.graph.workspaces import WorkspaceRepository

    repo = WorkspaceRepository(conn)
    ws = repo.get_by_slug(ws_str) or repo.get(ws_str)
    if ws is None:
        console.print(f"[red]no such workspace:[/red] {ws_str}")
        raise SystemExit(1)
    return ws


# ---------------------------------------------------------------------------
# Workspace commands
# ---------------------------------------------------------------------------


@workspace_app.command(name="create")
def workspace_create(
    slug: str,
    name: str | None = None,
    kind: str = "mixed",
    description: str = "",
) -> None:
    """Create an information workspace.

    kind: papers | codebase | notes | transcripts | web | mixed
    """
    from loci.db import init_schema
    from loci.db.connection import connect
    from loci.graph.models import Workspace, new_id, now_iso
    from loci.graph.workspaces import WorkspaceRepository

    init_schema()
    conn = connect()
    ws = WorkspaceRepository(conn).create(
        Workspace(
            id=new_id(),
            slug=slug,
            name=name or slug,
            description_md=description,
            kind=kind,
            created_at=now_iso(),
            last_active_at=now_iso(),
        )
    )
    conn.commit()
    console.print(f"[green]created workspace[/green] [bold]{ws.slug}[/bold] ({ws.id})")


@workspace_app.command(name="list")
def workspace_list() -> None:
    """List all information workspaces."""
    from loci.db import init_schema
    from loci.db.connection import connect
    from loci.graph.workspaces import WorkspaceRepository

    init_schema()
    conn = connect()
    wss = WorkspaceRepository(conn).list()
    if not wss:
        console.print("[dim]no workspaces[/dim]")
        return
    table = Table("slug", "name", "kind", "id", "last_scanned_at")
    for ws in wss:
        table.add_row(ws.slug, ws.name, ws.kind, ws.id, ws.last_scanned_at or "—")
    console.print(table)


@workspace_app.command(name="info")
def workspace_info(slug: str) -> None:
    """Show details for a workspace including linked projects and sources."""
    from loci.db import init_schema
    from loci.db.connection import connect
    from loci.graph.workspaces import WorkspaceRepository

    init_schema()
    conn = connect()
    ws = _resolve_workspace(conn, slug)
    repo = WorkspaceRepository(conn)
    sources = repo.list_sources(ws.id)
    console.print(
        {
            "id": ws.id,
            "slug": ws.slug,
            "name": ws.name,
            "kind": ws.kind,
            "last_scanned_at": ws.last_scanned_at,
        }
    )
    table = Table("id", "root_path", "label", "last_scanned_at")
    for s in sources:
        table.add_row(s.id, s.root_path, s.label or "", s.last_scanned_at or "—")
    console.print(table)


@workspace_app.command(name="add-source")
def workspace_add_source(workspace: str, root: Path, label: str | None = None) -> None:
    """Register a directory as a source root for a workspace."""
    from loci.db import init_schema
    from loci.db.connection import connect
    from loci.graph.workspaces import WorkspaceRepository

    init_schema()
    conn = connect()
    ws = _resolve_workspace(conn, workspace)
    src = WorkspaceRepository(conn).add_source(ws.id, root, label=label)
    conn.commit()
    console.print(f"[green]registered[/green] {src.root_path} (id={src.id})")


@workspace_app.command(name="link")
def workspace_link(workspace: str, project: str, role: str = "primary") -> None:
    """Link a workspace to a project.

    role: primary | reference | excluded
    """
    from loci.db import init_schema
    from loci.db.connection import connect
    from loci.graph.workspaces import WorkspaceRepository

    init_schema()
    conn = connect()
    ws = _resolve_workspace(conn, workspace)
    proj = _resolve_project(conn, project)
    WorkspaceRepository(conn).link_project(proj.id, ws.id, role=role)  # type: ignore[arg-type]
    conn.commit()
    console.print(
        f"[green]linked[/green] [bold]{ws.slug}[/bold] → [bold]{proj.slug}[/bold] (role={role})"
    )


@workspace_app.command(name="unlink")
def workspace_unlink(workspace: str, project: str) -> None:
    """Remove the link between a workspace and a project."""
    from loci.db import init_schema
    from loci.db.connection import connect
    from loci.graph.workspaces import WorkspaceRepository

    init_schema()
    conn = connect()
    ws = _resolve_workspace(conn, workspace)
    proj = _resolve_project(conn, project)
    WorkspaceRepository(conn).unlink_project(proj.id, ws.id)
    conn.commit()
    console.print(f"[yellow]unlinked[/yellow] {ws.slug} from {proj.slug}")


@workspace_app.command(name="scan")
def workspace_scan(workspace: str) -> None:
    """Scan all source roots registered to a workspace."""
    from loci.db import init_schema
    from loci.db.connection import connect
    from loci.ingest.pipeline import scan_workspace

    init_schema()
    conn = connect()
    ws = _resolve_workspace(conn, workspace)
    res = scan_workspace(conn, ws.id)
    console.print(
        {
            "scanned": res.scanned,
            "new_raw": res.new_raw,
            "deduped": res.deduped,
            "skipped": res.skipped,
            "members_added": res.members_added,
            "errors": res.errors[:5],
        }
    )


# ---------------------------------------------------------------------------
# scan (project-level)
# ---------------------------------------------------------------------------


@app.command
def scan(project: str) -> None:
    """Scan every workspace linked to a project (workspaces own source roots)."""
    from loci.db import init_schema
    from loci.db.connection import connect
    from loci.ingest.pipeline import scan_project

    init_schema()
    conn = connect()
    proj = _resolve_project(conn, project)
    res = scan_project(conn, proj.id)
    console.print(
        {
            "scanned": res.scanned,
            "new_raw": res.new_raw,
            "deduped": res.deduped,
            "skipped": res.skipped,
            "members_added": res.members_added,
            "errors": res.errors[:5],
        }
    )


# ---------------------------------------------------------------------------
# save — ingest a URL or file path
# ---------------------------------------------------------------------------


@app.command
def save(
    source: str,
    folder: str | None = None,
    aspects: str | None = None,
    context: str | None = None,
    project: str | None = None,
) -> None:
    """Save a resource (URL or file path) to loci."""
    from loci.db import init_schema
    from loci.db.connection import connect

    init_schema()
    conn = connect()
    project_id = _resolve_project_id_auto(conn, project)

    # Determine if source is URL or file path.
    is_url = source.startswith("http://") or source.startswith("https://")

    console.print(f"[dim]ingesting {'URL' if is_url else 'file'}: {source}[/dim]")

    if is_url:
        from loci.capture.ingest import ingest_url

        result = asyncio.run(
            ingest_url(
                url=source,
                context_text=context,
                project_id=project_id,
                conn=conn,
            )
        )
    else:
        from loci.capture.ingest import ingest_file

        result = asyncio.run(
            ingest_file(
                path=source,
                context_text=context,
                project_id=project_id,
                conn=conn,
            )
        )

    # Print summary.
    status_tag = "[yellow]duplicate[/yellow]" if result.is_duplicate else "[green]saved[/green]"
    console.print(f"{status_tag} [bold]{result.title}[/bold] (id={result.resource_id})")

    if result.is_duplicate:
        console.print(
            f"  already in DB — folder: {result.existing_folder or '(none)'}"
            f"  aspects: {', '.join(result.existing_aspects) or '(none)'}"
        )
        return

    # Determine confirmed folder.
    confirmed_folder = folder
    if confirmed_folder is None:
        if result.folder_suggestions:
            import questionary

            choices = [f for f, _ in result.folder_suggestions] + ["(skip)"]
            confirmed_folder = questionary.select(
                "Choose a folder for this resource:",
                choices=choices,
            ).ask()
            if confirmed_folder == "(skip)":
                confirmed_folder = None
        else:
            console.print("  [dim]no folder suggestions[/dim]")

    # Determine confirmed aspects.
    confirmed_aspects: list[str] = []
    if aspects is not None:
        confirmed_aspects = [a.strip() for a in aspects.split(",") if a.strip()]
    elif result.aspect_suggestions:
        import questionary

        confirmed_aspects = (
            questionary.checkbox(
                "Select aspect labels for this resource (space to toggle, enter to confirm):",
                choices=result.aspect_suggestions,
            ).ask()
            or []
        )
    else:
        console.print("  [dim]no aspect suggestions[/dim]")

    # Write confirmed folder and aspects to DB.
    if confirmed_folder is not None:
        from loci.graph.models import now_iso

        conn.execute(
            """
            INSERT OR REPLACE INTO resource_provenance
                (resource_id, folder, captured_at)
            VALUES (?, ?, ?)
            ON CONFLICT(resource_id) DO UPDATE SET folder = excluded.folder
            """,
            (result.resource_id, confirmed_folder, now_iso()),
        )

    if confirmed_aspects:
        from loci.graph.aspects import AspectRepository

        AspectRepository(conn).tag_resource(
            result.resource_id,
            confirmed_aspects,
            source="user",
            confidence=1.0,
        )

    conn.commit()

    console.print(f"  folder:  {confirmed_folder or '(none)'}")
    console.print(f"  aspects: {', '.join(confirmed_aspects) or '(none)'}")
    console.print(
        f"\n[dim]Use @loci:source://{result.resource_id} to reference this resource.[/dim]"
    )


# ---------------------------------------------------------------------------
# use — set active project/workspaces and show resource table
# ---------------------------------------------------------------------------


@app.command
def use(
    workspaces: list[str] | None = None,
    project: str | None = None,
) -> None:
    """Set the active project/workspaces for this session. Shows a rich table of available resources."""
    from loci.db import init_schema
    from loci.db.connection import connect

    init_schema()
    conn = connect()
    project_id = _resolve_project_id_auto(conn, project)

    # Fetch project info.
    from loci.graph import ProjectRepository

    proj = ProjectRepository(conn).get(project_id)
    if proj is None:
        console.print(f"[red]project not found for id:[/red] {project_id}")
        raise SystemExit(1)

    # Write session.toml if project or workspaces were given.
    if project is not None or workspaces:
        session_path = _session_toml_path()
        lines = ["[session]\n"]
        lines.append(f'project = "{proj.slug}"\n')
        if workspaces:
            ws_list = ", ".join(f'"{w}"' for w in workspaces)
            lines.append(f"workspaces = [{ws_list}]\n")
        session_path.write_text("".join(lines), encoding="utf-8")
        console.print(f"[green]session pinned[/green] → {session_path}")

    # Count sources.
    source_count_row = conn.execute(
        """
        SELECT COUNT(DISTINCT n.id) AS cnt
        FROM nodes n
        JOIN project_effective_members pm ON pm.node_id = n.id
        WHERE pm.project_id = ?
        """,
        (project_id,),
    ).fetchone()
    source_count = source_count_row["cnt"] if source_count_row else 0

    # Query resources grouped by folder with top aspects.
    folder_rows = conn.execute(
        """
        SELECT rp.folder, COUNT(DISTINCT n.id) AS cnt
        FROM nodes n
        JOIN project_effective_members pm ON pm.node_id = n.id
        LEFT JOIN resource_provenance rp ON rp.resource_id = n.id
        WHERE pm.project_id = ?
        GROUP BY rp.folder
        ORDER BY cnt DESC
        """,
        (project_id,),
    ).fetchall()

    from loci.graph.aspects import AspectRepository

    aspect_repo = AspectRepository(conn)
    top_global = aspect_repo.top_aspects(project_id, limit=3)
    top_labels = ", ".join(label for label, _ in top_global) if top_global else "—"

    # Build rich table.
    table = Table(
        show_header=True,
        header_style="bold",
        title=f"Project: [bold]{proj.name or proj.slug}[/bold]  |  {source_count} sources",
        show_lines=True,
    )
    table.add_column("Folder", style="cyan")
    table.add_column("Sources", justify="right")
    table.add_column("Top Aspects")

    for row in folder_rows:
        folder_label = row["folder"] or "(no folder)"
        # Get top 3 aspects for this folder's resources.
        aspect_rows = conn.execute(
            """
            SELECT av.label, COUNT(ra.resource_id) AS cnt
            FROM resource_aspects ra
            JOIN aspect_vocab av ON av.id = ra.aspect_id
            JOIN nodes n ON n.id = ra.resource_id
            JOIN project_effective_members pm ON pm.node_id = n.id
            LEFT JOIN resource_provenance rp ON rp.resource_id = n.id
            WHERE pm.project_id = ? AND (rp.folder = ? OR (rp.folder IS NULL AND ? IS NULL))
            GROUP BY av.id, av.label
            ORDER BY cnt DESC
            LIMIT 3
            """,
            (project_id, row["folder"], row["folder"]),
        ).fetchall()
        folder_aspects = ", ".join(r["label"] for r in aspect_rows) if aspect_rows else "—"
        table.add_row(folder_label, str(row["cnt"]), folder_aspects)

    console.print(table)
    console.print("\n[dim]Use @loci:source://<id> or loci_recall in Claude Code[/dim]")
    if top_global:
        console.print(f"[dim]Top aspects across project: {top_labels}[/dim]")


# ---------------------------------------------------------------------------
# recall — concept-graph-driven retrieval
# ---------------------------------------------------------------------------


@app.command
def recall(
    query: str,
    aspects: str | None = None,
    folder: str | None = None,
    n: int = 5,
    project: str | None = None,
    explain: bool = False,
) -> None:
    """Retrieve relevant resources using concept-graph-driven search.

    Pass --explain / -e to show the retrieval trace (expanded aspects, HyDE
    hypothesis, and per-result graph-boost indicator).
    """
    from loci.db import init_schema
    from loci.db.connection import connect
    from loci.retrieve.pipeline import RetrievalTrace, retrieve
    from loci.retrieve.query_cnl import split_query

    init_schema()
    conn = connect()
    project_id = _resolve_project_id_auto(conn, project)

    filter_aspects: list[str] | None = None
    if aspects:
        filter_aspects = [a.strip() for a in aspects.split(",") if a.strip()]

    free_text, cnl_query = split_query(query)
    effective_query = free_text if free_text else query

    retrieve_result = asyncio.run(
        retrieve(
            query=effective_query,
            project_id=project_id,
            conn=conn,
            n=n,
            filter_aspects=filter_aspects,
            filter_folder=folder,
            return_trace=explain,
            cnl_query=cnl_query if not cnl_query.is_empty else None,
        )
    )

    trace: RetrievalTrace | None = None
    if explain:
        results, trace = retrieve_result  # type: ignore[misc]
    else:
        results = retrieve_result  # type: ignore[assignment]

    if not results:
        console.print("[yellow]no results found[/yellow]")
        return

    from loci.graph.handles import short_id as _short_id

    for i, res in enumerate(results, start=1):
        sid = _short_id(res.resource_id)
        console.rule(f"[bold]{i}. {sid} — {res.title}[/bold]")
        folder_str = f"  folder: {res.folder}" if res.folder else ""
        aspects_str = f"  aspects: {', '.join(res.aspects)}" if res.aspects else ""
        if folder_str:
            console.print(folder_str)
        if aspects_str:
            console.print(aspects_str)
        console.print(f"  why: {res.why_surfaced}")
        if trace:
            boosted = res.resource_id in trace.boosted_resource_ids
            boost_note = "  [dim](graph-boosted)[/dim]" if boosted else ""
            console.print(f"  score: {res.total_score:.4f}{boost_note}")
        else:
            console.print(f"  score: {res.total_score:.4f}")
        if res.chunks:
            top_chunk = res.chunks[0]
            snippet = top_chunk.text[:300].replace("\n", " ")
            if top_chunk.section:
                console.print(f"  [{top_chunk.section}] {snippet}…")
            else:
                console.print(f"  {snippet}…")
            if trace and (top_chunk.lex_score or top_chunk.vec_score):
                console.print(
                    f"  [dim]BM25: {top_chunk.lex_score:.4f}  ANN: {top_chunk.vec_score:.4f}[/dim]"
                )
        console.print(f"  [dim]id: {res.resource_id}[/dim]")

    if trace:
        console.rule("[dim]Retrieval trace[/dim]")
        console.print(f'  [bold]Query:[/bold] "{query}"')
        if not cnl_query.is_empty:
            parts = []
            if cnl_query.topic:
                parts.append(f"topic={'~' if cnl_query.topic_fuzzy else ''}{cnl_query.topic}")
            if cnl_query.kind:
                parts.append(f"kind={cnl_query.kind}")
            if cnl_query.role:
                parts.append(f"role={cnl_query.role}")
            if cnl_query.target:
                parts.append(f"target={cnl_query.target}")
            console.print(f"  [bold]Parsed CNL:[/bold] {' '.join(parts)}")
        if trace.expanded_aspects:
            console.print(f"  [bold]ε(q)[/bold] = [{', '.join(trace.expanded_aspects)}]")
        if trace.hyde_hypothesis:
            hyp = trace.hyde_hypothesis[:200].replace("\n", " ")
            console.print(f"  [bold]HyDE:[/bold] {hyp!r} [dim][truncated at 200 chars][/dim]")


# ---------------------------------------------------------------------------
# aspects — view or edit aspect labels for a resource
# ---------------------------------------------------------------------------


@app.command
def aspects(
    resource_id: str | None = None,
    add: str | None = None,
    remove: str | None = None,
    list_vocab: bool = False,
    project: str | None = None,
) -> None:
    """View or edit aspect labels for a resource."""
    from loci.db import init_schema
    from loci.db.connection import connect
    from loci.graph.aspects import AspectRepository

    init_schema()
    conn = connect()
    aspect_repo = AspectRepository(conn)

    if list_vocab:
        project_id: str | None = None
        if project:
            proj = _resolve_project(conn, project)
            project_id = proj.id
        vocab = aspect_repo.list_vocab(project_id=project_id)
        if not vocab:
            console.print("[dim]no aspects in vocabulary[/dim]")
            return
        table = Table("label", "kind", "role", "target", "src", "last_used", "id")
        for a in vocab:
            table.add_row(
                a.label,
                a.kind or "—",
                a.role or "—",
                (a.target_aspect_id or "")[:12] + ("…" if a.target_aspect_id else "—"),
                "user" if a.user_defined else ("inferred" if a.auto_inferred else "—"),
                (a.last_used or "—")[:16],
                a.id[:12] + "…",
            )
        console.print(table)
        return

    if resource_id is None:
        console.print("[red]provide a resource_id or use --list-vocab[/red]")
        raise SystemExit(1)

    # Apply explicit add/remove first.
    if add:
        labels = [a.strip() for a in add.split(",") if a.strip()]
        aspect_repo.tag_resource(resource_id, labels, source="user", confidence=1.0)
        conn.commit()
        console.print(f"[green]added[/green] aspects: {', '.join(labels)}")

    if remove:
        labels = [a.strip() for a in remove.split(",") if a.strip()]
        aspect_repo.untag_resource(resource_id, labels)
        conn.commit()
        console.print(f"[yellow]removed[/yellow] aspects: {', '.join(labels)}")

    # Fetch and display current aspects.
    current = aspect_repo.aspects_for(resource_id)

    if not add and not remove:
        # Interactive mode: offer a checkbox toggle if questionary is available.
        if current:
            label_map = {ra.aspect_id: "" for ra in current}
            for ra in current:
                av = aspect_repo.get_by_id(ra.aspect_id)
                if av:
                    label_map[ra.aspect_id] = av.label
            current_label_list = [
                label_map[ra.aspect_id] for ra in current if label_map.get(ra.aspect_id)
            ]
        else:
            current_label_list = []

        # Show existing aspects.
        if current_label_list:
            console.print(f"Current aspects: {', '.join(current_label_list)}")
        else:
            console.print("[dim]no aspects set[/dim]")

        try:
            import questionary

            # Get full vocab for this project.
            project_id_str: str | None = None
            if project:
                p = _resolve_project(conn, project)
                project_id_str = p.id
            vocab = aspect_repo.list_vocab(project_id=project_id_str)
            vocab_labels = [a.label for a in vocab]
            if not vocab_labels:
                console.print(
                    "[dim]no vocabulary to choose from; use loci save to add resources first[/dim]"
                )
                return
            selected = questionary.checkbox(
                "Toggle aspects (space to select, enter to confirm):",
                choices=vocab_labels,
                default=current_label_list,
            ).ask()
            if selected is None:
                return  # Cancelled.
            # Compute diff.
            to_add = [label for label in selected if label not in current_label_list]
            to_remove = [label for label in current_label_list if label not in selected]
            if to_add:
                aspect_repo.tag_resource(resource_id, to_add, source="user", confidence=1.0)
            if to_remove:
                aspect_repo.untag_resource(resource_id, to_remove)
            if to_add or to_remove:
                conn.commit()
                console.print(f"[green]updated[/green] aspects for {resource_id[:12]}…")
        except ImportError:
            console.print("[dim]questionary not available; pass --add / --remove to edit[/dim]")
        return

    # Show updated state.
    updated = aspect_repo.aspects_for(resource_id)
    if updated:
        labels_now = []
        for ra in updated:
            av = aspect_repo.get_by_id(ra.aspect_id)
            if av:
                labels_now.append(av.label)
        console.print(f"Current aspects: {', '.join(labels_now)}")
    else:
        console.print("[dim]no aspects set[/dim]")


# ---------------------------------------------------------------------------
# aspect trace — show aspect provenance history for a resource
# ---------------------------------------------------------------------------


@aspect_app.command(name="trace")
def aspect_trace(
    handle: str,
    project: str | None = None,
) -> None:
    """Show the aspect provenance history for a resource.

    <handle> can be a short-id (rid_XXXXXX), full UUID, or fuzzy title.
    """
    from rich.tree import Tree

    from loci.db import init_schema
    from loci.db.connection import connect
    from loci.graph.handles import resolve_handle, short_id as _short_id

    init_schema()
    conn = connect()
    project_id = _resolve_project_id_auto(conn, project) if project else None

    # Resolve handle to resource UUID.
    resource_id = resolve_handle(handle, project_id, conn)
    if resource_id is None:
        console.print(f"[red]could not resolve handle:[/red] {handle}")
        raise SystemExit(1)

    # Fetch title + short_id.
    node_row = conn.execute(
        "SELECT title FROM nodes WHERE id = ?", (resource_id,)
    ).fetchone()
    title = (node_row["title"] if node_row else resource_id) or resource_id

    sid = _short_id(resource_id)

    # Check if aspect_provenance table exists.
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "aspect_provenance" not in tables:
        console.print(f"[yellow]{sid} — {title!r}[/yellow]")
        console.print("[dim]No aspect provenance data yet (migration pending).[/dim]")
        return

    # Query provenance rows for this resource.
    rows = conn.execute(
        """
        SELECT ap.aspect_id, av.label,
               ap.action, ap.source, ap.confidence, ap.rationale, ap.recorded_at
        FROM aspect_provenance ap
        JOIN aspect_vocab av ON av.id = ap.aspect_id
        WHERE ap.resource_id = ?
          AND (ap.project_id IS ? OR ap.project_id IS NULL)
        ORDER BY ap.recorded_at ASC
        """,
        (resource_id, project_id),
    ).fetchall()

    if not rows:
        console.print(f"[bold]{sid} — {title!r}[/bold]")
        console.print("[dim]No provenance entries for this resource.[/dim]")
        return

    # Group rows by aspect_id.
    from collections import defaultdict

    by_aspect: dict[str, list] = defaultdict(list)
    aspect_labels: dict[str, str] = {}
    for row in rows:
        aid = row["aspect_id"]
        aspect_labels[aid] = row["label"]
        by_aspect[aid].append(row)

    # Sort aspects by latest recorded_at desc.
    sorted_aspects = sorted(
        by_aspect.items(),
        key=lambda kv: max(r["recorded_at"] for r in kv[1]),
        reverse=True,
    )

    # Source color mapping.
    source_colors = {
        "user": "cyan",
        "folder": "green",
        "llm": "magenta",
        "infer_interpretation": "magenta",
        "inferred": "dim",
        "seed": "bright_black",
        "usage": "yellow",
        "conversation": "blue",
    }

    tree = Tree(f"[bold]{sid} — {title!r}[/bold]")

    for aid, aspect_rows in sorted_aspects:
        label = aspect_labels[aid]

        # Latest row determines the "current" source + confidence.
        latest = aspect_rows[-1]
        src = latest["source"]
        conf = latest["confidence"]
        conf_str = f"{conf:.2f}" if conf is not None else "—"
        color = source_colors.get(src, "white")
        lock_icon = " [cyan]🔒[/cyan]" if src == "user" else " [magenta]✨[/magenta]" if src in ("llm", "inferred", "infer_interpretation") else ""

        branch = tree.add(
            f"[{color}]{label}[/{color}]  "
            f"(source: {src}, conf: {conf_str}){lock_icon}"
        )

        for entry in aspect_rows:
            action = entry["action"]
            entry_src = entry["source"]
            entry_color = source_colors.get(entry_src, "white")
            date_str = (entry["recorded_at"] or "")[:10]
            rationale = entry["rationale"]
            rationale_str = f" · [dim]{rationale[:60]}[/dim]" if rationale else ""
            branch.add(
                f"[dim]{action}[/dim] · "
                f"[{entry_color}]{entry_src}[/{entry_color}] · "
                f"[dim]{date_str}[/dim]{rationale_str}"
            )

    console.print(tree)


# ---------------------------------------------------------------------------
# aspects graph — render aspect-to-aspect relationship tree
# ---------------------------------------------------------------------------


@aspect_app.command(name="graph")
def aspect_graph(
    root: str | None = None,
    depth: int = 2,
    project: str | None = None,
) -> None:
    """Render the aspect graph (aspect_edges) as a rich tree.

    Use --root <label> to start from a specific aspect, or leave blank
    to use the top-5 most-used aspects for the project.
    """
    from rich.tree import Tree

    from loci.db import init_schema
    from loci.db.connection import connect
    from loci.graph.aspects import AspectRepository

    init_schema()
    conn = connect()
    project_id = _resolve_project_id_auto(conn, project) if project else None

    # Check if aspect_edges exists.
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "aspect_edges" not in tables:
        console.print("[yellow]No aspect graph data yet.[/yellow]")
        console.print("[dim]Aspect edges are computed after aspects are classified.[/dim]")
        return

    aspect_repo = AspectRepository(conn)

    # Helper: resource count for an aspect in this project.
    def _resource_count(label: str) -> int:
        if project_id is None:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM resource_aspects ra "
                "JOIN aspect_vocab av ON av.id = ra.aspect_id WHERE av.label = ?",
                (label,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM project_resource_aspects pra
                JOIN aspect_vocab av ON av.id = pra.aspect_id
                WHERE av.label = ? AND pra.project_id = ?
                """,
                (label, project_id),
            ).fetchone()
        return row["c"] if row else 0

    # Determine root aspects.
    if root:
        root_labels = [root]
    else:
        if project_id:
            top = aspect_repo.top_aspects(project_id, limit=5)
        else:
            rows = conn.execute(
                "SELECT av.label, COUNT(ra.resource_id) AS cnt "
                "FROM resource_aspects ra JOIN aspect_vocab av ON av.id = ra.aspect_id "
                "GROUP BY av.label ORDER BY cnt DESC LIMIT 5"
            ).fetchall()
            top = [(r["label"], r["cnt"]) for r in rows]
        root_labels = [label for label, _ in top]

    if not root_labels:
        console.print("[dim]No aspects found.[/dim]")
        return

    # Helper: fetch outgoing edges for an aspect label.
    def _edges(label: str) -> list[dict]:
        av = aspect_repo.get_by_label(label, project_id=project_id) or aspect_repo.get_by_label(label)
        if av is None:
            return []
        rows = conn.execute(
            """
            SELECT av2.label AS dst_label, ae.edge_type, ae.weight
            FROM aspect_edges ae
            JOIN aspect_vocab av2 ON av2.id = ae.dst_aspect_id
            WHERE ae.src_aspect_id = ?
              AND (ae.project_id IS ? OR ae.project_id IS NULL)
            ORDER BY ae.weight DESC
            LIMIT 10
            """,
            (av.id, project_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def _build_tree(branch, label: str, current_depth: int, visited: set) -> None:
        if current_depth <= 0 or label in visited:
            return
        visited.add(label)
        for edge in _edges(label):
            dst = edge["dst_label"]
            etype = edge["edge_type"]
            weight = edge["weight"]
            cnt = _resource_count(dst)
            child = branch.add(
                f"[cyan]{etype}[/cyan] → [bold]{dst}[/bold]  "
                f"([dim]{cnt} res, w={weight:.2f}[/dim])"
            )
            _build_tree(child, dst, current_depth - 1, visited)

    tree = Tree("[bold]Aspect Graph[/bold]")
    for label in root_labels:
        cnt = _resource_count(label)
        branch = tree.add(f"[bold]{label}[/bold]  ([dim]{cnt} resources[/dim])")
        _build_tree(branch, label, depth, {label})

    console.print(tree)


# ---------------------------------------------------------------------------
# aspect structure — batch-upgrade flat labels to CNL propositions
# ---------------------------------------------------------------------------


@aspect_app.command(name="structure")
def aspect_structure(
    project: str,
    limit: int = 20,
    yes: bool = False,
) -> None:
    """Re-interpret flat aspect labels as typed CNL propositions (costs LLM tokens).

    Finds resources in the project whose aspects have no structured kind/role/target
    columns and re-triggers `infer_interpretation` with force=True so the LLM
    re-emits propositions in CNL format. Run `loci worker` to process queued jobs.

    Use --limit to cap how many resources are scheduled (default 20).
    """
    from loci.db import init_schema
    from loci.db.connection import connect
    from loci.jobs.queue import enqueue

    init_schema()
    conn = connect()
    proj = _resolve_project(conn, project)
    project_id = proj.id

    # Find resources with at least one flat aspect (kind IS NULL) in this project.
    rows = conn.execute(
        """
        SELECT DISTINCT pra.resource_id, n.title
        FROM project_resource_aspects pra
        JOIN aspect_vocab av ON av.id = pra.aspect_id
        JOIN raw_nodes n ON n.id = pra.resource_id
        WHERE pra.project_id = ?
          AND av.kind IS NULL
        ORDER BY pra.updated_at DESC
        LIMIT ?
        """,
        (project_id, limit),
    ).fetchall()

    if not rows:
        console.print("[green]All aspects in this project are already structured.[/green]")
        return

    console.print(
        f"[yellow]Will re-interpret {len(rows)} resource(s) via LLM (costs tokens):[/yellow]"
    )
    for row in rows[:5]:
        title_str = (row["title"] or row["resource_id"])[:60]
        console.print(f"  • {title_str}")
    if len(rows) > 5:
        console.print(f"  … and {len(rows) - 5} more")

    if not yes:
        ans = input("Proceed? [y/N] ").strip().lower()
        if ans != "y":
            console.print("[yellow]aborted[/yellow]")
            return

    from loci.graph.models import now_iso

    ts = now_iso()
    count = 0
    for row in rows:
        rid = row["resource_id"]
        fingerprint = f"structure:{project_id}:{rid}:{ts}"[:64]
        enqueue(
            conn,
            kind="infer_interpretation",
            project_id=project_id,
            payload={"resource_id": rid, "project_id": project_id, "trigger": "structure", "force": True},
            fingerprint=fingerprint,
        )
        count += 1
    conn.commit()
    console.print(
        f"[green]Enqueued {count} infer_interpretation job(s) with force=True.[/green]"
    )
    console.print("[dim]Run `loci worker` to process them.[/dim]")


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@app.command
def doctor() -> None:
    """Print resolved storage paths, settings sources, and active project."""
    import os

    from loci.mcp.resolve import find_project_file

    settings = get_settings()

    console.print("[bold]loci doctor[/bold]")
    console.print()
    console.print("[bold]Data root[/bold]")
    table = Table(show_header=False, box=None, padding=(0, 2))
    paths = {
        "data_dir": settings.data_dir,
        "db": settings.db_path,
        "blobs": settings.blob_dir,
        "models": settings.model_cache_dir,
        "logs": settings.logs_dir,
        "exports": settings.exports_dir,
        "state": settings.state_dir,
    }
    for name, path in paths.items():
        exists = "[green]ok[/green]" if path.exists() else "[dim]–[/dim]"
        table.add_row(exists, name, str(path))
    console.print(table)

    console.print()
    console.print("[bold]Settings sources[/bold]")
    sources = []
    env_loci = os.environ.get("LOCI_DATA_DIR")
    if env_loci:
        sources.append(f"LOCI_DATA_DIR={env_loci} (env var)")
    cwd_env = Path(".env")
    if cwd_env.exists():
        sources.append(f"{cwd_env.resolve()} (cwd .env)")
    home_env = settings.data_dir / ".env"
    if home_env.exists():
        sources.append(f"{home_env} (~/.loci/.env)")
    home_toml = settings.data_dir / "config.toml"
    if home_toml.exists():
        sources.append(f"{home_toml} (~/.loci/config.toml)")
    for s in sources:
        console.print(f"  {s}")
    if not sources:
        console.print("  [dim]only defaults / environment variables[/dim]")

    console.print()
    console.print("[bold]Active project[/bold]")
    cwd_slug = find_project_file()
    env_slug = os.environ.get("LOCI_PROJECT")
    state_path = _state_file_path()
    state_slug = state_path.read_text(encoding="utf-8").strip() if state_path.exists() else None
    if env_slug:
        console.print(f"  LOCI_PROJECT={env_slug}")
    if cwd_slug:
        console.print(f"  .loci/project walk-up → {cwd_slug}")
    if state_slug:
        console.print(f"  state/current → {state_slug}")
    if not any([env_slug, cwd_slug, state_slug]):
        console.print("  [dim]none (pass project= or run `loci project bind <slug>`)[/dim]")


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------


@app.command
def reset(yes: bool = False) -> None:
    """Wipe the loci database. Destructive — drops every node, edge, project,
    workspace, response, and job. Re-creates the schema empty.

    Pass --yes to skip the confirmation prompt.
    """
    settings = get_settings()
    db_path = settings.db_path
    blob_dir = settings.blob_dir
    console.print(f"[red]This will delete:[/red]\n  • {db_path}\n  • all blobs under {blob_dir}")
    if not yes:
        ans = input("Type 'wipe' to confirm: ").strip().lower()
        if ans != "wipe":
            console.print("[yellow]aborted[/yellow]")
            return
    import shutil

    if db_path.exists():
        db_path.unlink()
    # WAL/SHM sidecars
    for sidecar in (
        db_path.with_suffix(db_path.suffix + "-wal"),
        db_path.with_suffix(db_path.suffix + "-shm"),
    ):
        if sidecar.exists():
            sidecar.unlink()
    if blob_dir.exists():
        shutil.rmtree(blob_dir)
    blob_dir.mkdir(parents=True, exist_ok=True)
    # Re-create schema empty so the next CLI call doesn't trip the migration runner.
    from loci.db import init_schema

    init_schema()
    console.print(
        "[green]reset complete[/green] — run `loci workspace create`, "
        "`loci workspace add-source`, `loci workspace scan`, "
        "`loci project create`, `loci workspace link`."
    )


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@app.command
def status(project: str | None = None) -> None:
    """Show graph counts and (with a project slug) resource summary."""
    from loci.db import init_schema
    from loci.db.connection import connect

    init_schema()
    conn = connect()

    if project:
        from loci.graph import ProjectRepository

        proj = ProjectRepository(conn).get_by_slug(project) or ProjectRepository(conn).get(project)
        if proj is None:
            console.print(f"[red]no such project:[/red] {project}")
            raise SystemExit(1)
        nm = conn.execute(
            "SELECT COUNT(*) AS c FROM project_membership WHERE project_id = ?", (proj.id,)
        ).fetchone()["c"]
        queued = conn.execute("SELECT COUNT(*) AS c FROM jobs WHERE status='queued'").fetchone()[
            "c"
        ]
        console.rule(f"[bold]{proj.slug}[/bold]")
        console.print(f"  {nm} nodes  ·  {queued} jobs queued")
        return

    rows = []
    rows.append(("nodes", "", str(conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])))
    rows.append(("projects", "", str(conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0])))
    rows.append(
        (
            "jobs",
            "queued",
            str(conn.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]),
        )
    )
    rows.append(
        (
            "jobs",
            "done",
            str(conn.execute("SELECT COUNT(*) FROM jobs WHERE status='done'").fetchone()[0]),
        )
    )
    table = Table("entity", "filter", "count")
    for e, f, c in rows:
        table.add_row(e, f, c)
    console.print(table)


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


@app.command
def export(
    project: str | None = None,
    to: Path | None = None,
) -> None:
    """Export a resource summary (JSON) for a project.

    When run from a directory bound to a project (via .loci/project),
    writes to <repo>/.loci/views/. Otherwise writes to ~/.loci/exports/.
    """
    import datetime
    import json as _json

    from loci.db import init_schema
    from loci.db.connection import connect
    from loci.graph import ProjectRepository
    from loci.graph.aspects import AspectRepository
    from loci.mcp.resolve import find_project_file

    init_schema()
    conn = connect()
    settings = get_settings()

    # Resolve project.
    if project is None:
        project = find_project_file()
    if project is None:
        console.print("[red]no project specified and no .loci/project found[/red]")
        raise SystemExit(1)

    proj = ProjectRepository(conn).get_by_slug(project) or ProjectRepository(conn).get(project)
    if proj is None:
        console.print(f"[red]no such project:[/red] {project}")
        raise SystemExit(1)

    # Resolve output directory.
    if to is not None:
        views_dir = to
    else:
        loci_dir = Path.cwd() / ".loci"
        if loci_dir.is_dir():
            views_dir = loci_dir / "views"
        else:
            ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d-%H%M%S")
            views_dir = settings.exports_dir / f"{proj.slug}-{ts}"
    views_dir.mkdir(parents=True, exist_ok=True)

    # Build resource summary.
    aspect_repo = AspectRepository(conn)
    rows = conn.execute(
        """
        SELECT n.id, n.title, n.subkind, rp.folder, rp.source_url, rp.captured_at
        FROM nodes n
        JOIN project_effective_members pm ON pm.node_id = n.id
        LEFT JOIN resource_provenance rp ON rp.resource_id = n.id
        WHERE pm.project_id = ?
        ORDER BY rp.captured_at DESC NULLS LAST
        """,
        (proj.id,),
    ).fetchall()

    resources = []
    for row in rows:
        resource_aspects = [ra.aspect_id for ra in aspect_repo.aspects_for(row["id"])]
        # Resolve labels.
        labels = []
        for aid in resource_aspects:
            av = aspect_repo.get_by_id(aid)
            if av:
                labels.append(av.label)
        resources.append(
            {
                "id": row["id"],
                "title": row["title"],
                "subkind": row["subkind"],
                "folder": row["folder"],
                "source_url": row["source_url"],
                "captured_at": row["captured_at"],
                "aspects": labels,
            }
        )

    payload = {
        "project": {"id": proj.id, "slug": proj.slug, "name": proj.name},
        "resources": resources,
        "exported_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }

    out_path = views_dir / "resources.json"
    out_path.write_text(_json.dumps(payload, ensure_ascii=False, indent=2))
    console.print(f"[green]wrote[/green] {out_path}  ({len(resources)} resources)")


# ---------------------------------------------------------------------------
# event — ingest external signals (Claude Code conversation hooks, etc.)
# ---------------------------------------------------------------------------


def _maybe_trigger_inference(conn, project_id: str, text: str, settings) -> None:
    """Enqueue infer_interpretation for resources whose aspects appear in text."""
    try:
        from rapidfuzz import fuzz
        from rapidfuzz import process as rfprocess
    except ImportError:
        return

    from datetime import UTC, datetime

    from loci.graph.aspects import AspectRepository
    from loci.jobs.queue import enqueue

    aspect_repo = AspectRepository(conn)
    top = aspect_repo.top_aspects(project_id, limit=20)
    if not top:
        return

    labels = [label for label, _ in top]
    hits = rfprocess.extract(
        text[:500],
        labels,
        scorer=fuzz.partial_ratio,
        limit=5,
        score_cutoff=settings.conversation_relevance_cutoff,
    )
    if not hits:
        return

    matched_labels = [label for label, _score, _idx in hits]
    today = datetime.now(UTC).strftime("%Y-%m-%d")

    for label in matched_labels[:3]:
        rids = aspect_repo.resources_for_aspect(label, project_id=project_id, limit=5)
        for rid in rids:
            fingerprint = f"infer_interpretation:{project_id}:{rid}:{today}"[:64]
            enqueue(
                conn,
                kind="infer_interpretation",
                project_id=project_id,
                payload={
                    "resource_id": rid,
                    "project_id": project_id,
                    "trigger": "conversation",
                },
                fingerprint=fingerprint,
            )


@event_app.command(name="conversation")
def event_conversation(
    role: str = "user",
    project: str | None = None,
    cwd: str | None = None,
) -> None:
    """Ingest a Claude Code conversation event from stdin (JSON hook payload).

    Reads JSON from stdin. Claude Code hook payload format:
      {"prompt": "...", "cwd": "/path/to/repo"}  (UserPromptSubmit)
      {"response": "...", "cwd": "/path/to/repo"}  (Stop hook)

    Exits silently if capture_conversation is disabled or no project resolved.
    """
    import json
    import re
    import sys

    settings = get_settings()

    if not settings.capture_conversation:
        return  # opt-in required

    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError):
        return

    text = payload.get("prompt") or payload.get("response") or ""
    if not text:
        return

    # Redact and truncate
    text = re.sub(settings.conversation_redact_paths, "[REDACTED]", text)
    text = text[: settings.conversation_max_chars]

    effective_cwd = cwd or payload.get("cwd")

    try:
        from pathlib import Path as _Path

        from loci.db import init_schema
        from loci.db.connection import connect
        from loci.graph.models import new_id, now_iso
        from loci.mcp.resolve import ProjectNotFound, resolve_project_id

        init_schema()
        conn = connect()

        cwd_path = _Path(effective_cwd) if effective_cwd else None
        try:
            project_id = resolve_project_id(conn, project, cwd=cwd_path)
        except ProjectNotFound:
            return  # no project → nothing to do

    except Exception:  # noqa: BLE001
        return

    if not project_id:
        return

    # Derive a session hash from pid + cwd (stable per Claude Code session)
    import hashlib
    import os

    session_hash = hashlib.sha256(
        f"{os.getpid()}:{effective_cwd or ''}".encode()
    ).hexdigest()[:16]

    try:
        event_id = new_id()
        conn.execute(
            "INSERT INTO conversation_events(id, project_id, session_hash, role, text, cwd, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, project_id, session_hash, role, text, effective_cwd, now_iso()),
        )

        # Lightweight relevance check: fuzzy-match text against project top aspects
        _maybe_trigger_inference(conn, project_id, text, settings)
    except Exception:  # noqa: BLE001
        pass  # never surface errors from a hook
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> None:  # script entrypoint (pyproject `loci = "loci.cli:main"`)
    app()


if __name__ == "__main__":
    main()
