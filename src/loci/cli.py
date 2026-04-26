"""loci CLI.

Powered by cyclopts (>=3) for typed CLIs without ceremony.

Subcommands:
    loci server [--host] [--port] [--no-worker]
    loci mcp                              MCP stdio server (for Claude Code)
    loci worker [--poll-interval]
    loci project create <slug>            interactive setup wizard
    loci project manage                   edit / delete existing projects
    loci project bind <slug>              write .loci/project in cwd
    loci project list
    loci project info <slug>
    loci workspace create/list/info/add-source/link/unlink/scan
    loci scan <project>                   scan all linked workspaces
    loci q <project> <query> [--k] [--hyde]
    loci draft <project> <instruction> [--style] [--cite-density]
    loci kickoff <project> [--n]          seed the interpretation graph
    loci reflect <project>                manual reflect cycle
    loci absorb <project>
    loci graph export <project> [--output FILE]
    loci rebuild <project>
    loci reset
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
workspace_app = App(name="workspace", help="Information workspace commands.")
app.command(workspace_app)
graph_app = App(name="graph", help="Export graph visualizations.")
app.command(graph_app)


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

    from loci.db import migrate
    from loci.db.connection import connect

    migrate()
    conn = connect()

    if sys.stdin.isatty() and not yes:
        from loci.tui import run_wizard
        run_wizard(conn, slug_hint=slug)
    else:
        from loci.graph import Project, ProjectRepository
        profile_md = profile.read_text() if profile else ""
        proj = ProjectRepository(conn).create(Project(
            slug=slug, name=name or slug, profile_md=profile_md,
        ))
        conn.commit()
        console.print(f"[green]created[/green] [bold]{proj.slug}[/bold] ({proj.id})")


@project_app.command(name="manage")
def project_manage() -> None:
    """Open the interactive TUI project manager (list, edit, delete, create)."""
    from loci.db import migrate
    from loci.db.connection import connect
    from loci.tui import run_wizard

    migrate()
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
def scan(project: str) -> None:
    """Scan every workspace linked to a project (workspaces own source roots)."""
    from loci.db import migrate
    from loci.db.connection import connect
    from loci.ingest.pipeline import scan_project

    migrate()
    conn = connect()
    proj = _resolve_project(conn, project)
    res = scan_project(conn, proj.id)
    console.print({"scanned": res.scanned, "new_raw": res.new_raw,
                    "deduped": res.deduped, "skipped": res.skipped,
                    "members_added": res.members_added,
                    "errors": res.errors[:5]})


def _resolve_project(conn, project_str: str):
    from loci.graph import ProjectRepository
    repo = ProjectRepository(conn)
    proj = repo.get_by_slug(project_str) or repo.get(project_str)
    if proj is None:
        console.print(f"[red]no such project:[/red] {project_str}")
        raise SystemExit(1)
    return proj


# ---------------------------------------------------------------------------
# Workspace commands
# ---------------------------------------------------------------------------


def _resolve_workspace(conn, ws_str: str):
    from loci.graph.workspaces import WorkspaceRepository
    repo = WorkspaceRepository(conn)
    ws = repo.get_by_slug(ws_str) or repo.get(ws_str)
    if ws is None:
        console.print(f"[red]no such workspace:[/red] {ws_str}")
        raise SystemExit(1)
    return ws


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
    from loci.db import migrate
    from loci.db.connection import connect
    from loci.graph.models import Workspace, new_id, now_iso
    from loci.graph.workspaces import WorkspaceRepository

    migrate()
    conn = connect()
    ws = WorkspaceRepository(conn).create(Workspace(
        id=new_id(), slug=slug, name=name or slug,
        description_md=description, kind=kind,
        created_at=now_iso(), last_active_at=now_iso(),
    ))
    conn.commit()
    console.print(f"[green]created workspace[/green] [bold]{ws.slug}[/bold] ({ws.id})")


@workspace_app.command(name="list")
def workspace_list() -> None:
    """List all information workspaces."""
    from loci.db import migrate
    from loci.db.connection import connect
    from loci.graph.workspaces import WorkspaceRepository

    migrate()
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
    from loci.db import migrate
    from loci.db.connection import connect
    from loci.graph.workspaces import WorkspaceRepository

    migrate()
    conn = connect()
    ws = _resolve_workspace(conn, slug)
    repo = WorkspaceRepository(conn)
    sources = repo.list_sources(ws.id)
    console.print({"id": ws.id, "slug": ws.slug, "name": ws.name,
                    "kind": ws.kind, "last_scanned_at": ws.last_scanned_at})
    table = Table("id", "root_path", "label", "last_scanned_at")
    for s in sources:
        table.add_row(s.id, s.root_path, s.label or "", s.last_scanned_at or "—")
    console.print(table)


@workspace_app.command(name="add-source")
def workspace_add_source(workspace: str, root: Path, label: str | None = None) -> None:
    """Register a directory as a source root for a workspace."""
    from loci.db import migrate
    from loci.db.connection import connect
    from loci.graph.workspaces import WorkspaceRepository

    migrate()
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
    from loci.db import migrate
    from loci.db.connection import connect
    from loci.graph.workspaces import WorkspaceRepository

    migrate()
    conn = connect()
    ws = _resolve_workspace(conn, workspace)
    proj = _resolve_project(conn, project)
    WorkspaceRepository(conn).link_project(proj.id, ws.id, role=role)  # type: ignore[arg-type]
    conn.commit()
    console.print(f"[green]linked[/green] [bold]{ws.slug}[/bold] → [bold]{proj.slug}[/bold] (role={role})")


@workspace_app.command(name="unlink")
def workspace_unlink(workspace: str, project: str) -> None:
    """Remove the link between a workspace and a project."""
    from loci.db import migrate
    from loci.db.connection import connect
    from loci.graph.workspaces import WorkspaceRepository

    migrate()
    conn = connect()
    ws = _resolve_workspace(conn, workspace)
    proj = _resolve_project(conn, project)
    WorkspaceRepository(conn).unlink_project(proj.id, ws.id)
    conn.commit()
    console.print(f"[yellow]unlinked[/yellow] {ws.slug} from {proj.slug}")


@workspace_app.command(name="scan")
def workspace_scan(workspace: str) -> None:
    """Scan all source roots registered to a workspace."""
    from loci.db import migrate
    from loci.db.connection import connect
    from loci.ingest.pipeline import scan_workspace

    migrate()
    conn = connect()
    ws = _resolve_workspace(conn, workspace)
    res = scan_workspace(conn, ws.id)
    console.print({"scanned": res.scanned, "new_raw": res.new_raw,
                    "deduped": res.deduped, "skipped": res.skipped,
                    "members_added": res.members_added,
                    "errors": res.errors[:5]})


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
    # Top-line: the raws this query landed on.
    table = Table("kind", "subkind", "title", "score", "why", show_lines=False)
    for n in resp.nodes:
        table.add_row(n.kind, n.subkind, n.title, f"{n.score:.4f}", n.why)
    console.print(table)

    # Routing loci side panel — the loci of thought used to reach those raws.
    if resp.routing_interps:
        console.rule("routing loci of thought")
        rl_table = Table("subkind", "title", "angle", "score", show_lines=False)
        for ri in resp.routing_interps[:10]:
            rl_table.add_row(
                ri.subkind, ri.title, ri.angle or "—", f"{ri.score:.4f}",
            )
        console.print(rl_table)

    # Trace table — for each raw, the interp path that routed to it.
    if resp.trace_table:
        console.rule("trace (raw ← locus path)")
        tr_table = Table("raw", "interp path", show_lines=False)
        nodes_by_id = {ri.node_id: ri for ri in resp.routing_interps}
        for row in resp.trace_table[:k]:
            path_strs = []
            seen: set[str] = set()
            for hop in row["interp_path"]:
                for iid in (hop["id"], hop["to"] if hop["edge"] == "derives_from" else None):
                    if iid and iid not in seen:
                        seen.add(iid)
                        ri = nodes_by_id.get(iid)
                        path_strs.append(f"[{ri.subkind if ri else '?'}] {ri.title if ri else iid[:8]}")
            tr_table.add_row(row["raw_title"][:60], " → ".join(path_strs) or "—")
        console.print(tr_table)


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
    console.rule("citations (raws)")
    nodes_by_id = {rl.node_id: rl for rl in res.routing_loci}
    for c in res.citations:
        line = f"  [{c.kind}/{c.subkind}] {c.title!r} — {c.why_cited}"
        if c.routed_by:
            routers = []
            for iid in c.routed_by[:3]:
                rl = nodes_by_id.get(iid)
                routers.append(f"{rl.subkind}:{rl.title}" if rl else iid[:8])
            line += f"\n    routed_via: {' → '.join(routers)}"
        console.print(line)
    if res.routing_loci:
        console.rule("loci of thought (routing context)")
        for rl in res.routing_loci[:8]:
            console.print(
                f"  [{rl.subkind}{f'/{rl.angle}' if rl.angle else ''}] {rl.title}\n"
                f"    relation: {rl.relation_md[:160]}"
            )
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


@graph_app.command(name="json")
def graph_json(
    project: str,
    output: Path | None = None,
    include_raw: bool = True,
) -> None:
    """Write the graph payload as JSON (nodes + edges).

    Omit --output to print to stdout. The JSON is the same shape the frontend
    force-directed layout consumes: nodes[]{id,kind,subkind,title,body,cited_raws[]}
    and edges[]{source,target,type,weight}.
    """
    import json as _json

    from loci.db import migrate
    from loci.db.connection import connect
    from loci.graph import ProjectRepository
    from loci.graph.export import build_graph_payload

    migrate()
    conn = connect()
    proj = ProjectRepository(conn).get_by_slug(project) or ProjectRepository(conn).get(project)
    if proj is None:
        console.print(f"[red]no such project:[/red] {project}")
        raise SystemExit(1)
    payload = build_graph_payload(proj, conn, include_raw=include_raw)
    out_str = _json.dumps(payload, ensure_ascii=False, indent=2)
    if output:
        output.write_text(out_str)
        console.print(f"[green]wrote[/green] {output}")
    else:
        print(out_str)


@graph_app.command(name="export")
def graph_export(
    project: str,
    output: Path = Path("/tmp/loci_graph.html"),
    include_raw: bool = True,
) -> None:
    """Write a standalone HTML graph snapshot for a project."""
    from loci.db import migrate
    from loci.db.connection import connect
    from loci.graph import ProjectRepository
    from loci.graph.export import build_graph_payload, write_graph_html

    migrate()
    conn = connect()
    proj = ProjectRepository(conn).get_by_slug(project) or ProjectRepository(conn).get(project)
    if proj is None:
        console.print(f"[red]no such project:[/red] {project}")
        raise SystemExit(1)
    out = write_graph_html(proj, conn, output, include_raw=include_raw)
    console.print(f"[green]wrote[/green] {out}")

    # Diagnostics when the graph looks thin
    payload = build_graph_payload(proj, conn, include_raw=include_raw)
    stats = payload["stats"]
    console.print(
        f"  [dim]{stats['total_nodes']} nodes "
        f"({stats['raw_nodes']} raw · {stats['interpretation_nodes']} interp) "
        f"· {stats['edges']} edges[/dim]"
    )
    if stats["edges"] == 0 and stats["raw_nodes"] > 0:
        console.print("[yellow]⚠  No edges found.[/yellow] Possible reasons:")
        linked = conn.execute(
            "SELECT COUNT(*) FROM project_workspaces WHERE project_id = ?", (proj.id,)
        ).fetchone()[0]
        interp_count = conn.execute(
            "SELECT COUNT(*) FROM nodes n JOIN project_membership pm ON pm.node_id = n.id"
            " WHERE pm.project_id = ? AND n.kind = 'interpretation'", (proj.id,)
        ).fetchone()[0]
        if linked == 0:
            console.print(
                f"  1. No workspace linked — run: [cyan]loci workspace link <ws> {proj.slug}[/cyan]"
            )
        if interp_count == 0:
            console.print(
                f"  2. No interpretation nodes — run: [cyan]loci kickoff {proj.slug}[/cyan]"
            )
        if linked > 0 and interp_count == 0:
            console.print(
                "     (kickoff may have skipped if workspace was empty when it ran; "
                "re-run after scanning)"
            )


@app.command
def kickoff(project: str, n: int = 8) -> None:
    """Seed the interpretation graph with relationship observations.

    Reads the project profile + a sample of the workspace's raws and writes
    5–8 live interpretation nodes (relevance, philosophy, decision) at
    confidence 0.5. Runs synchronously (CLI-blocking).
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
def reset(yes: bool = False) -> None:
    """Wipe the loci database. Destructive — drops every node, edge, project,
    workspace, response, and job. Re-creates the schema empty.

    Use this after a schema rewrite (e.g. the DAG migration) when you want a
    clean slate. Pass --yes to skip the confirmation prompt.
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
    for sidecar in (db_path.with_suffix(db_path.suffix + "-wal"),
                    db_path.with_suffix(db_path.suffix + "-shm")):
        if sidecar.exists():
            sidecar.unlink()
    if blob_dir.exists():
        shutil.rmtree(blob_dir)
    blob_dir.mkdir(parents=True, exist_ok=True)
    # Re-create schema empty so the next CLI call doesn't trip the migration runner.
    from loci.db import migrate
    migrate()
    console.print("[green]reset complete[/green] — run `loci workspace create`, "
                  "`loci workspace add-source`, `loci workspace scan`, "
                  "`loci project create`, `loci workspace link`, `loci kickoff`.")


@app.command
def rebuild(project: str, n: int = 6) -> None:
    """Re-link, re-scan, and re-interpret a project from scratch.

    Drops every interpretation node currently in this project, re-scans every
    workspace linked to it, then runs kickoff to regenerate the loci-of-thought
    layer. Raw nodes and workspace memberships are preserved (the user's
    sources are the source of truth). Use this after the DAG migration to
    regenerate loci with the new prompt + schema.
    """
    from loci.db import migrate
    from loci.db.connection import connect, transaction
    from loci.graph.workspaces import WorkspaceRepository
    from loci.ingest.pipeline import scan_workspace
    from loci.jobs import enqueue
    from loci.jobs.queue import get_job
    from loci.jobs.worker import run_once

    migrate()
    conn = connect()
    proj = _resolve_project(conn, project)

    # Drop interpretation nodes that belong to this project. Edge FKs cascade.
    with transaction(conn):
        conn.execute(
            """
            DELETE FROM nodes WHERE id IN (
                SELECT n.id FROM nodes n
                JOIN project_membership pm ON pm.node_id = n.id
                WHERE pm.project_id = ? AND n.kind = 'interpretation'
            )
            """,
            (proj.id,),
        )
    interp_dropped = conn.total_changes
    console.print(f"[yellow]dropped {interp_dropped} interpretation node(s)[/yellow]")

    # Re-scan every linked workspace (raws are content-addressed — re-scan is idempotent).
    ws_repo = WorkspaceRepository(conn)
    links = ws_repo.linked_workspaces(proj.id)
    for ws, link in links:
        if link.role == "excluded":
            continue
        res = scan_workspace(conn, ws.id)
        console.print(f"[green]rescanned[/green] {ws.slug}: "
                      f"new_raw={res.new_raw} deduped={res.deduped} skipped={res.skipped}")

    # Kick off the loci-of-thought generation.
    jid = enqueue(conn, kind="kickoff", project_id=proj.id, payload={"n": n})
    console.print(f"[dim]enqueued kickoff {jid}; running...[/dim]")
    run_once(conn)
    console.print(get_job(conn, jid))


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
