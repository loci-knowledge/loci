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
) -> None:
    """Retrieve relevant resources using concept-graph-driven search."""
    from loci.db import init_schema
    from loci.db.connection import connect
    from loci.retrieve.pipeline import retrieve

    init_schema()
    conn = connect()
    project_id = _resolve_project_id_auto(conn, project)

    filter_aspects: list[str] | None = None
    if aspects:
        filter_aspects = [a.strip() for a in aspects.split(",") if a.strip()]

    results = asyncio.run(
        retrieve(
            query=query,
            project_id=project_id,
            conn=conn,
            n=n,
            filter_aspects=filter_aspects,
            filter_folder=folder,
        )
    )

    if not results:
        console.print("[yellow]no results found[/yellow]")
        return

    for i, res in enumerate(results, start=1):
        console.rule(f"[bold]{i}. {res.title}[/bold]")
        folder_str = f"  folder: {res.folder}" if res.folder else ""
        aspects_str = f"  aspects: {', '.join(res.aspects)}" if res.aspects else ""
        if folder_str:
            console.print(folder_str)
        if aspects_str:
            console.print(aspects_str)
        console.print(f"  why: {res.why_surfaced}")
        console.print(f"  score: {res.total_score:.4f}")
        if res.chunks:
            top_chunk = res.chunks[0]
            snippet = top_chunk.text[:300].replace("\n", " ")
            if top_chunk.section:
                console.print(f"  [{top_chunk.section}] {snippet}…")
            else:
                console.print(f"  {snippet}…")
        console.print(f"  [dim]id: {res.resource_id}[/dim]")


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
        table = Table("label", "user_defined", "auto_inferred", "last_used", "id")
        for a in vocab:
            table.add_row(
                a.label,
                "yes" if a.user_defined else "—",
                "yes" if a.auto_inferred else "—",
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


def main() -> None:  # script entrypoint (pyproject `loci = "loci.cli:main"`)
    app()


if __name__ == "__main__":
    main()
