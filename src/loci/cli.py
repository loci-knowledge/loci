"""loci CLI.

Powered by cyclopts (>=3) for typed CLIs without ceremony.

Subcommands:
    loci server [--host] [--port] [--no-worker]
    loci mcp                              MCP stdio server
    loci worker [--poll-interval]
    loci project create <slug> [--name] [--profile FILE]
    loci project list
    loci project info <slug>
    loci scan <project> <root>            ingest a directory
    loci q <project> <query> [--k] [--hyde]
    loci draft <project> <instruction> [--style] [--cite-density]
    loci absorb <project>
    loci status [project]
"""

from __future__ import annotations

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
    help="Personal memory graph server. See PLAN.md.",
    version=__version__,
)
project_app = App(name="project", help="Project commands.")
app.command(project_app)
source_app = App(name="source", help="Manage scan roots for a project.")
app.command(source_app)


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
) -> None:
    """Create a project. Reads the profile markdown from FILE if provided."""
    from loci.db import migrate
    from loci.db.connection import connect
    from loci.graph import Project, ProjectRepository

    migrate()
    conn = connect()
    profile_md = profile.read_text() if profile else ""
    proj = ProjectRepository(conn).create(Project(
        slug=slug, name=name or slug, profile_md=profile_md,
    ))
    console.print(f"[green]created[/green] [bold]{proj.slug}[/bold] ({proj.id})")


@project_app.command(name="list")
def project_list() -> None:
    """List all projects."""
    from loci.db import migrate
    from loci.db.connection import connect
    from loci.graph import ProjectRepository

    migrate()
    conn = connect()
    table = Table("slug", "name", "id", "last_active_at")
    for p in ProjectRepository(conn).list():
        table.add_row(p.slug, p.name, p.id, p.last_active_at)
    console.print(table)


@project_app.command(name="info")
def project_info(slug: str) -> None:
    """Show details for one project."""
    from loci.db import migrate
    from loci.db.connection import connect
    from loci.graph import ProjectRepository

    migrate()
    conn = connect()
    proj = ProjectRepository(conn).get_by_slug(slug)
    if proj is None:
        console.print(f"[red]no such project:[/red] {slug}")
        raise SystemExit(1)
    members = conn.execute(
        "SELECT COUNT(*) AS c FROM project_membership WHERE project_id = ?",
        (proj.id,),
    ).fetchone()["c"]
    console.print({"id": proj.id, "slug": proj.slug, "name": proj.name,
                    "members": members, "last_active_at": proj.last_active_at})
    if proj.profile_md:
        console.rule("profile")
        console.print(proj.profile_md)


# ---------------------------------------------------------------------------
# Scan / query / draft / absorb / status
# ---------------------------------------------------------------------------


@app.command
def scan(project: str, root: Path | None = None) -> None:
    """Walk a directory and ingest every supported file into a project.

    If `root` is omitted, walks every registered source for the project
    (see `loci source add`).
    """
    from loci.db import migrate
    from loci.db.connection import connect
    from loci.graph import ProjectRepository
    from loci.ingest import scan_path, scan_registered_sources

    migrate()
    conn = connect()
    proj = ProjectRepository(conn).get_by_slug(project) or ProjectRepository(conn).get(project)
    if proj is None:
        console.print(f"[red]no such project:[/red] {project}")
        raise SystemExit(1)
    res = (
        scan_path(conn, proj.id, root)
        if root is not None
        else scan_registered_sources(conn, proj.id)
    )
    console.print({"scanned": res.scanned, "new_raw": res.new_raw,
                    "deduped": res.deduped, "skipped": res.skipped,
                    "members_added": res.members_added,
                    "errors": res.errors[:5]})


# ---------------------------------------------------------------------------
# Source-root commands
# ---------------------------------------------------------------------------


def _resolve_project(conn, project_str: str):
    from loci.graph import ProjectRepository
    repo = ProjectRepository(conn)
    proj = repo.get_by_slug(project_str) or repo.get(project_str)
    if proj is None:
        console.print(f"[red]no such project:[/red] {project_str}")
        raise SystemExit(1)
    return proj


@source_app.command(name="add")
def source_add(project: str, root: Path, label: str | None = None) -> None:
    """Register a directory as a scan root for the project."""
    from loci.db import migrate
    from loci.db.connection import connect
    from loci.graph import SourceRepository

    migrate()
    conn = connect()
    proj = _resolve_project(conn, project)
    src = SourceRepository(conn).add(proj.id, root, label=label)
    console.print(f"[green]registered[/green] {src.root_path} (id={src.id})")


@source_app.command(name="list")
def source_list(project: str) -> None:
    """List registered scan roots for a project."""
    from loci.db import migrate
    from loci.db.connection import connect
    from loci.graph import SourceRepository

    migrate()
    conn = connect()
    proj = _resolve_project(conn, project)
    srcs = SourceRepository(conn).list(proj.id)
    if not srcs:
        console.print("[dim]no sources registered[/dim]")
        return
    table = Table("id", "root_path", "label", "last_scanned_at")
    for s in srcs:
        table.add_row(s.id, s.root_path, s.label or "", s.last_scanned_at or "—")
    console.print(table)


@source_app.command(name="remove")
def source_remove(project: str, source: str) -> None:
    """Remove a scan root by id or path."""
    from loci.db import migrate
    from loci.db.connection import connect
    from loci.graph import SourceRepository

    migrate()
    conn = connect()
    proj = _resolve_project(conn, project)
    ok = SourceRepository(conn).remove(proj.id, source)
    console.print("[green]removed[/green]" if ok else "[red]not found[/red]")


@app.command(name="q")
def query(
    project: str,
    query: str,
    k: int = 10,
    hyde: bool = False,
) -> None:
    """Retrieve from a project."""
    from loci.db import migrate
    from loci.db.connection import connect
    from loci.graph import ProjectRepository
    from loci.retrieve import RetrievalRequest, Retriever

    migrate()
    conn = connect()
    proj = ProjectRepository(conn).get_by_slug(project) or ProjectRepository(conn).get(project)
    if proj is None:
        console.print(f"[red]no such project:[/red] {project}")
        raise SystemExit(1)
    resp = Retriever(conn).retrieve(RetrievalRequest(
        project_id=proj.id, query=query, k=k, hyde=hyde,
    ))
    table = Table("kind", "subkind", "title", "score", "why", show_lines=False)
    for n in resp.nodes:
        table.add_row(n.kind, n.subkind, n.title, f"{n.score:.4f}", n.why)
    console.print(table)


@app.command
def draft(
    project: str,
    instruction: str,
    style: str = "prose",
    cite_density: str = "normal",
    k: int = 12,
    hyde: bool = False,
) -> None:
    """Generate a draft with citations from a project."""
    from loci.db import migrate
    from loci.db.connection import connect
    from loci.draft import DraftRequest
    from loci.draft import draft as run_draft
    from loci.graph import ProjectRepository

    migrate()
    conn = connect()
    proj = ProjectRepository(conn).get_by_slug(project) or ProjectRepository(conn).get(project)
    if proj is None:
        console.print(f"[red]no such project:[/red] {project}")
        raise SystemExit(1)
    res = run_draft(conn, DraftRequest(
        project_id=proj.id, session_id="cli", instruction=instruction,
        style=style, cite_density=cite_density,  # type: ignore[arg-type]
        k=k, hyde=hyde, client="cli",
    ))
    console.rule("draft")
    console.print(res.output_md)
    console.rule("citations")
    for c in res.citations:
        console.print(f"  [{c.kind}/{c.subkind}] {c.title!r} — {c.why_cited}")
    console.print(f"\n[dim]response_id: {res.response_id}[/dim]")


@app.command
def feedback(response_id: str, edited_markdown_file: Path) -> None:
    """Submit citation-level feedback for a previous draft.

    Pass a path to your edited version of the draft markdown. loci diffs the
    [Cn] markers and emits per-citation traces (kept/dropped/replaced), then
    enqueues a follow-up reflection that aligns the interpretation layer with
    your actual usage.
    """
    from loci.agent import diff_citations, emit_feedback_traces
    from loci.api.routes.feedback import _recover_handle_map
    from loci.citations import CitationTracker
    from loci.db import migrate
    from loci.db.connection import connect
    from loci.jobs import enqueue
    from loci.jobs.queue import get_job
    from loci.jobs.worker import run_once

    migrate()
    conn = connect()
    rec = CitationTracker(conn).get_response(response_id)
    if rec is None:
        console.print(f"[red]no such response:[/red] {response_id}")
        raise SystemExit(1)
    handle_to_id = _recover_handle_map(rec)
    if not handle_to_id:
        console.print("[yellow]response has no [Cn] citations to diff[/yellow]")
        raise SystemExit(1)
    edited_md = edited_markdown_file.read_text()
    diffs = diff_citations(rec["output"], edited_md, handle_to_id)
    counts = emit_feedback_traces(conn, rec["project_id"], response_id, diffs)
    console.print({"counts": counts, "diffs": [d.__dict__ for d in diffs]})
    jid = enqueue(
        conn, kind="reflect", project_id=rec["project_id"],
        payload={"response_id": response_id, "trigger": "feedback"},
    )
    console.print(f"[dim]enqueued reflect {jid}; running...[/dim]")
    run_once(conn)
    console.print(get_job(conn, jid))


@app.command
def reflect(project: str, response_id: str | None = None) -> None:
    """Manually run a reflection cycle against a project.

    Pass `response_id` to reflect on a specific draft; omit to run a manual
    reflection from the project's pinned set + latest activity.
    """
    from loci.db import migrate
    from loci.db.connection import connect
    from loci.jobs import enqueue
    from loci.jobs.queue import get_job
    from loci.jobs.worker import run_once

    migrate()
    conn = connect()
    proj = _resolve_project(conn, project)
    jid = enqueue(
        conn, kind="reflect", project_id=proj.id,
        payload={"response_id": response_id, "trigger": "manual"},
    )
    console.print(f"[dim]enqueued {jid}; running...[/dim]")
    run_once(conn)
    console.print(get_job(conn, jid))


@app.command
def absorb(project: str) -> None:
    """Enqueue and run an absorb checkpoint synchronously (CLI-blocking)."""
    from loci.db import migrate
    from loci.db.connection import connect
    from loci.jobs import enqueue
    from loci.jobs.queue import get_job
    from loci.jobs.worker import run_once

    migrate()
    conn = connect()
    proj = _resolve_project(conn, project)
    jid = enqueue(conn, kind="absorb", project_id=proj.id)
    console.print(f"[dim]enqueued {jid}; running...[/dim]")
    run_once(conn)
    j = get_job(conn, jid)
    console.print(j)


@app.command
def kickoff(project: str, n: int = 8) -> None:
    """Generate the first set of question proposals for a project.

    Runs synchronously (CLI-blocking). Uses `interpretation_model`. After
    completion, see proposals via `loci status <project>` or the REST
    endpoint `GET /projects/:id/proposals`.
    """
    from loci.db import migrate
    from loci.db.connection import connect
    from loci.jobs import enqueue
    from loci.jobs.queue import get_job
    from loci.jobs.worker import run_once

    migrate()
    conn = connect()
    proj = _resolve_project(conn, project)
    jid = enqueue(conn, kind="kickoff", project_id=proj.id, payload={"n": n})
    console.print(f"[dim]enqueued kickoff {jid}; running...[/dim]")
    run_once(conn)
    j = get_job(conn, jid)
    console.print(j)


@app.command
def status(project: str | None = None) -> None:
    """Show counts: nodes, edges, projects, jobs, traces."""
    from loci.db import migrate
    from loci.db.connection import connect

    migrate()
    conn = connect()
    rows = []
    if project:
        from loci.graph import ProjectRepository
        proj = ProjectRepository(conn).get_by_slug(project) or ProjectRepository(conn).get(project)
        if proj is None:
            console.print(f"[red]no such project:[/red] {project}")
            raise SystemExit(1)
        nm = conn.execute("SELECT COUNT(*) AS c FROM project_membership WHERE project_id = ?", (proj.id,)).fetchone()["c"]
        rows.append(("project", proj.slug, str(nm)))
    rows.append(("nodes", "", str(conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])))
    rows.append(("edges", "", str(conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0])))
    rows.append(("projects", "", str(conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0])))
    rows.append(("traces", "", str(conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0])))
    rows.append(("responses", "", str(conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0])))
    rows.append(("jobs", "queued", str(conn.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0])))
    rows.append(("jobs", "done", str(conn.execute("SELECT COUNT(*) FROM jobs WHERE status='done'").fetchone()[0])))
    rows.append(("proposals", "pending", str(conn.execute("SELECT COUNT(*) FROM proposals WHERE status='pending'").fetchone()[0])))
    table = Table("entity", "filter", "count")
    for e, f, c in rows:
        table.add_row(e, f, c)
    console.print(table)


def main() -> None:  # script entrypoint (pyproject `loci = "loci.cli:main"`)
    app()


if __name__ == "__main__":
    main()
