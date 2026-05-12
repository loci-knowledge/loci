"""loci CLI.

Powered by cyclopts (>=3) for typed CLIs without ceremony.

Subcommands:
    loci config init                      write ~/.loci/.env + config.toml
    loci server [--host] [--port] [--no-worker]
    loci mcp                              MCP stdio server (for Claude Code)
    loci worker [--poll-interval]
    loci workspace create/list/info/add-source/scan
    loci doctor                           print resolved storage paths
    loci reset
    loci status [project]
    loci export [project]                 write resource summary
    loci event conversation               ingest Claude Code hook payload from stdin
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
workspace_app = App(name="workspace", help="Information workspace commands.")
app.command(workspace_app)
event_app = App(name="event", help="Ingest external signals into loci.")
app.command(event_app)


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
# Helpers
# ---------------------------------------------------------------------------


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
    *sources: Path,
    name: str | None = None,
    kind: str = "mixed",
    description: str = "",
) -> None:
    """Create an information workspace, optionally registering source roots immediately.

    kind: papers | codebase | notes | transcripts | web | mixed

    Examples:
        loci workspace create notes
        loci workspace create notes ~/Notes ~/Research --kind notes
    """
    from loci.db import init_schema
    from loci.db.connection import connect
    from loci.graph.models import Workspace, new_id, now_iso
    from loci.graph.workspaces import WorkspaceRepository

    init_schema()
    conn = connect()
    repo = WorkspaceRepository(conn)
    ws = repo.create(
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
    for src_path in sources:
        src = repo.add_source(ws.id, src_path)
        console.print(f"  [dim]registered source[/dim] {src.root_path}")
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
# doctor
# ---------------------------------------------------------------------------


@app.command
def doctor() -> None:
    """Print resolved storage paths and settings sources."""
    import os

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
    console.print("[dim]Project binding: run /loci in Claude Code to bind a workspace.[/dim]")


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
        "[green]reset complete[/green] — run `loci workspace create <slug>`, "
        "`loci workspace add-source <slug> <path>`, `loci workspace scan <slug>`, "
        "then open Claude Code and run /loci to bind the workspace."
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

    init_schema()
    conn = connect()
    settings = get_settings()

    # Resolve project — require explicit slug.
    if project is None:
        console.print("[red]no project specified — pass a project slug[/red]")
        raise SystemExit(1)

    proj = ProjectRepository(conn).get_by_slug(project) or ProjectRepository(conn).get(project)
    if proj is None:
        console.print(f"[red]no such project:[/red] {project}")
        raise SystemExit(1)

    # Resolve output directory.
    if to is not None:
        views_dir = to
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

        cwd_str = str(_Path(effective_cwd).resolve()) if effective_cwd else None
        try:
            project_id = resolve_project_id(conn, project, cwd=cwd_str)
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
