"""Lightweight terminal wizard for loci project setup.

create-next-app style: linear prompts → review summary → apply.
Any step can be re-done from the review screen before applying.

Uses questionary (arrow-key prompts) + rich (formatted output).
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import questionary
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

console = Console()

STYLE = questionary.Style([
    ("qmark", "fg:#00BFFF bold"),
    ("question", "bold"),
    ("answer", "fg:#00FF7F bold"),
    ("pointer", "fg:#00BFFF bold"),
    ("highlighted", "fg:#00BFFF bold"),
    ("selected", "fg:#00FF7F"),
    ("instruction", "fg:#555555"),
    ("separator", "fg:#444444"),
])


# ─────────────────────────────────────────────────────────────────────────────
# Shared state
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _State:
    project_id: str | None = None       # None = create new
    slug: str = ""
    name: str = ""
    profile_md: str = ""
    workspace_links: dict[str, str] = field(default_factory=dict)  # ws_id → role
    do_scan: bool = True
    do_kickoff: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_wizard(conn: sqlite3.Connection, slug_hint: str = "") -> None:
    """Launch the wizard.  Blocks until done or the user cancels."""
    from loci.graph import ProjectRepository

    _print_banner()
    try:
        if slug_hint:
            existing = ProjectRepository(conn).get_by_slug(slug_hint)
            if existing:
                console.print(f"[yellow]⚠[/yellow]  Project [bold]{slug_hint}[/bold] already exists.")
                edit = questionary.confirm(
                    f"Edit '{slug_hint}' instead?", default=True, style=STYLE,
                ).ask()
                if edit is None:
                    return
                if edit:
                    _full_flow(conn, _load_state(conn, existing.id))
            else:
                _full_flow(conn, _State(slug=slug_hint, name=slug_hint))
        else:
            _manage_menu(conn)
    except KeyboardInterrupt:
        console.print("\n[dim]Cancelled.[/dim]")


# ─────────────────────────────────────────────────────────────────────────────
# Menus
# ─────────────────────────────────────────────────────────────────────────────

def _manage_menu(conn: sqlite3.Connection) -> None:
    """Top-level menu for `loci project manage`."""
    from loci.graph import ProjectRepository

    while True:
        _show_projects_table(conn)
        projects = ProjectRepository(conn).list()

        choices = [questionary.Choice("+ Create new project", value="new")]
        for p in projects:
            choices.append(questionary.Choice(f"  Edit  {p.slug}  — {p.name}", value=f"edit:{p.id}"))
        if projects:
            choices.append(questionary.Choice("  Delete a project…", value="delete"))
        choices.append(questionary.Choice("  Exit", value="exit"))

        action = questionary.select("What would you like to do?", choices=choices, style=STYLE).ask()

        if action is None or action == "exit":
            return
        elif action == "new":
            _full_flow(conn, _State())
        elif isinstance(action, str) and action.startswith("edit:"):
            _full_flow(conn, _load_state(conn, action[5:]))
        elif action == "delete":
            _delete_flow(conn)
        console.print()


def _delete_flow(conn: sqlite3.Connection) -> None:
    from loci.graph import ProjectRepository

    projects = ProjectRepository(conn).list()
    if not projects:
        console.print("[dim]No projects to delete.[/dim]")
        return

    choices = [questionary.Choice(f"{p.slug}  —  {p.name}", value=p.id) for p in projects]
    choices.append(questionary.Choice("← Back", value="back"))

    proj_id = questionary.select("Select project to delete:", choices=choices, style=STYLE).ask()
    if proj_id is None or proj_id == "back":
        return

    proj = ProjectRepository(conn).get(proj_id)
    if not proj:
        return

    confirmed = questionary.confirm(
        f"Delete '{proj.slug}'? This cannot be undone.",
        default=False, style=STYLE,
    ).ask()

    if confirmed:
        ProjectRepository(conn).delete(proj_id)
        conn.commit()
        console.print(f"[red]✗[/red]  Deleted [bold]{proj.slug}[/bold]")


# ─────────────────────────────────────────────────────────────────────────────
# Full create / edit flow
# ─────────────────────────────────────────────────────────────────────────────

def _full_flow(conn: sqlite3.Connection, state: _State) -> None:
    console.print()
    console.rule("[dim]Project setup[/dim]")
    console.print()

    _step_name(state)
    _step_slug(conn, state)
    _step_profile(state)
    _step_workspaces(conn, state)
    _step_scan_kickoff(state)

    # Review loop — the user can change any answer before applying
    while True:
        console.print()
        _print_summary(conn, state)
        console.print()

        action = questionary.select(
            "Apply, or change something?",
            choices=[
                questionary.Choice("✓  Apply", value="apply"),
                questionary.Separator(),
                questionary.Choice("  Change name", value="name"),
                questionary.Choice("  Change slug", value="slug"),
                questionary.Choice("  Change profile", value="profile"),
                questionary.Choice("  Change workspace links", value="workspaces"),
                questionary.Choice("  Change scan / kickoff", value="scan"),
                questionary.Separator(),
                questionary.Choice("✗  Cancel without saving", value="cancel"),
            ],
            style=STYLE,
        ).ask()

        if action is None or action == "cancel":
            return
        if action == "apply":
            break
        if action == "name":
            _step_name(state)
            _step_slug(conn, state, force=False)
        elif action == "slug":
            _step_slug(conn, state, force=True)
        elif action == "profile":
            _step_profile(state)
        elif action == "workspaces":
            _step_workspaces(conn, state)
        elif action == "scan":
            _step_scan_kickoff(state)

    _apply(conn, state)


# ─────────────────────────────────────────────────────────────────────────────
# Steps
# ─────────────────────────────────────────────────────────────────────────────

def _step_name(state: _State) -> None:
    name = questionary.text("Project name:", default=state.name, style=STYLE).ask()
    if name is not None:
        state.name = name.strip() or state.name


def _step_slug(conn: sqlite3.Connection, state: _State, force: bool = True) -> None:
    default = _slugify(state.name) if (force or not state.slug) else state.slug
    while True:
        slug = questionary.text(
            "Slug  (lowercase, hyphens only):", default=default, style=STYLE,
        ).ask()
        if slug is None:
            return
        slug = slug.strip()
        if not slug:
            continue
        if not re.match(r"^[a-z0-9][a-z0-9-]*$", slug):
            console.print("[yellow]  Slug: lowercase letters, numbers, hyphens only.[/yellow]")
            default = slug
            continue
        from loci.graph import ProjectRepository
        existing = ProjectRepository(conn).get_by_slug(slug)
        if existing and existing.id != state.project_id:
            console.print(f"[yellow]  Slug '{slug}' is already taken by '{existing.name}'.[/yellow]")
            default = slug
            continue
        state.slug = slug
        return


def _step_profile(state: _State) -> None:
    choices = [
        questionary.Choice("Load from a .md file", value="file"),
        questionary.Choice("Enter a quick one-line description", value="quick"),
        questionary.Choice("Skip for now", value="skip"),
    ]
    if state.profile_md:
        choices.insert(0, questionary.Choice("Keep current profile", value="keep"))

    choice = questionary.select("Project profile:", choices=choices, style=STYLE).ask()

    if choice is None or choice in ("skip", "keep"):
        return
    if choice == "file":
        path_str = questionary.text(
            "Path to profile .md file:", style=STYLE,
        ).ask()
        if path_str:
            p = Path(path_str.strip()).expanduser()
            if p.exists():
                state.profile_md = p.read_text()
                console.print(f"  [dim]Loaded {len(state.profile_md)} chars from {p.name}[/dim]")
            else:
                console.print(f"  [yellow]File not found: {p}[/yellow]")
    elif choice == "quick":
        desc = questionary.text(
            "Brief description (you can expand it in a file later):",
            default=state.profile_md if "\n" not in state.profile_md else "",
            style=STYLE,
        ).ask()
        if desc:
            state.profile_md = desc.strip()


def _step_workspaces(conn: sqlite3.Connection, state: _State) -> None:
    from loci.graph.workspaces import WorkspaceRepository

    workspaces = WorkspaceRepository(conn).list()

    wants = questionary.confirm(
        "Link a workspace to this project?",
        default=bool(workspaces),
        style=STYLE,
    ).ask()

    if not wants:
        state.workspace_links = {}
        return

    if not workspaces:
        if questionary.confirm("No workspaces exist. Create one now?", default=True, style=STYLE).ask():
            ws = _create_workspace_inline(conn)
            if ws:
                workspaces = WorkspaceRepository(conn).list()
        if not workspaces:
            return

    # Multi-select with checkboxes
    ws_choices = [
        questionary.Choice(
            f"{ws.slug}  [{ws.kind}]  {ws.name}",
            value=ws.id,
            checked=ws.id in state.workspace_links,
        )
        for ws in workspaces
    ]
    ws_choices.append(questionary.Choice("+ Create new workspace", value="__new__"))

    selected = questionary.checkbox(
        "Select workspaces to link  (Space to toggle, Enter to confirm):",
        choices=ws_choices,
        style=STYLE,
    ).ask()

    if selected is None:
        return

    # Handle inline workspace creation
    if "__new__" in selected:
        selected.remove("__new__")
        ws = _create_workspace_inline(conn)
        if ws:
            selected.append(ws.id)
            workspaces = WorkspaceRepository(conn).list()

    # Ask role for each selected workspace
    new_links: dict[str, str] = {}
    for ws_id in selected:
        ws = next((w for w in workspaces if w.id == ws_id), None)
        if ws is None:
            ws = WorkspaceRepository(conn).get(ws_id)
        label = ws.slug if ws else ws_id
        role = questionary.select(
            f"Role for '{label}':",
            choices=["primary", "reference", "excluded"],
            default=state.workspace_links.get(ws_id, "primary"),
            style=STYLE,
        ).ask()
        new_links[ws_id] = role or "primary"

    state.workspace_links = new_links


def _create_workspace_inline(conn: sqlite3.Connection):
    """Prompt for a new workspace and create it immediately. Returns Workspace or None."""
    from loci.graph.models import Workspace, new_id, now_iso
    from loci.graph.workspaces import WorkspaceRepository

    slug = questionary.text("New workspace slug:", style=STYLE).ask()
    if not slug:
        return None
    slug = slug.strip()

    ws_repo = WorkspaceRepository(conn)
    if ws_repo.get_by_slug(slug):
        console.print(f"[yellow]  Slug '{slug}' already exists.[/yellow]")
        return None

    name = questionary.text("Name:", default=slug, style=STYLE).ask()
    kind = questionary.select(
        "Kind:",
        choices=["mixed", "papers", "codebase", "notes", "transcripts", "web"],
        default="mixed",
        style=STYLE,
    ).ask()
    source = questionary.text(
        "Source root path  (press Enter to skip):",
        default="",
        style=STYLE,
    ).ask()

    ws = Workspace(
        id=new_id(), slug=slug, name=(name or slug),
        description_md="", kind=(kind or "mixed"),
        created_at=now_iso(), last_active_at=now_iso(),
    )
    ws_repo.create(ws)
    if source and source.strip():
        ws_repo.add_source(ws.id, Path(source.strip()).expanduser())
    conn.commit()
    console.print(f"  [green]✓[/green]  Workspace [bold]{slug}[/bold] created")
    return ws


def _step_scan_kickoff(state: _State) -> None:
    if state.workspace_links:
        ans = questionary.confirm(
            "Scan linked workspaces after setup?", default=True, style=STYLE,
        ).ask()
        state.do_scan = bool(ans)
    else:
        state.do_scan = False

    ans = questionary.confirm(
        "Run kickoff to seed interpretation graph?",
        default=state.do_scan,
        style=STYLE,
    ).ask()
    state.do_kickoff = bool(ans)


# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────

def _print_summary(conn: sqlite3.Connection, state: _State) -> None:
    from loci.graph.workspaces import WorkspaceRepository

    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column(style="dim", min_width=14)
    t.add_column()

    action = "Update" if state.project_id else "Create"
    t.add_row(f"{action} project", f"[bold]{state.name}[/bold]  [dim][{state.slug}][/dim]")

    profile_preview = (state.profile_md[:80] + "…") if len(state.profile_md) > 80 else state.profile_md
    t.add_row("Profile", profile_preview or "[dim]none[/dim]")

    if state.workspace_links:
        ws_repo = WorkspaceRepository(conn)
        lines = []
        for ws_id, role in state.workspace_links.items():
            ws = ws_repo.get(ws_id)
            label = ws.slug if ws else ws_id
            lines.append(f"[bold]{label}[/bold]  [dim]({role})[/dim]")
        t.add_row("Workspaces", "\n".join(lines))
    else:
        t.add_row("Workspaces", "[dim]none[/dim]")

    t.add_row("Scan", "[green]yes[/green]" if state.do_scan else "[dim]no[/dim]")
    t.add_row("Kickoff", "[green]yes[/green]" if state.do_kickoff else "[dim]no[/dim]")

    console.print(Panel(t, title="[bold]Summary[/bold]", border_style="cyan", expand=False))


# ─────────────────────────────────────────────────────────────────────────────
# Apply
# ─────────────────────────────────────────────────────────────────────────────

def _apply(conn: sqlite3.Connection, state: _State) -> None:
    from loci.graph import Project, ProjectRepository
    from loci.graph.workspaces import WorkspaceRepository

    console.print()
    proj_repo = ProjectRepository(conn)
    ws_repo = WorkspaceRepository(conn)

    # 1. Create or update project
    if state.project_id is None:
        proj = Project(slug=state.slug, name=state.name, profile_md=state.profile_md)
        proj_repo.create(proj)
        conn.commit()
        state.project_id = proj.id
        console.print(f"[green]✓[/green]  Created project [bold]{proj.slug}[/bold]  [dim]{proj.id}[/dim]")
    else:
        proj_repo.update(state.project_id, state.slug, state.name, state.profile_md)
        conn.commit()
        console.print(f"[green]✓[/green]  Updated project [bold]{state.slug}[/bold]")

    # 2. Sync workspace links
    existing_ids = {ws.id for ws, _ in ws_repo.linked_workspaces(state.project_id)}
    for ws_id in existing_ids - set(state.workspace_links):
        ws_repo.unlink_project(state.project_id, ws_id)
        ws = ws_repo.get(ws_id)
        console.print(f"[yellow]−[/yellow]  Unlinked {ws.slug if ws else ws_id}")
    for ws_id, role in state.workspace_links.items():
        ws = ws_repo.get(ws_id)
        if ws:
            ws_repo.link_project(state.project_id, ws_id, role=role)  # type: ignore[arg-type]
            console.print(f"[green]✓[/green]  Linked [bold]{ws.slug}[/bold] as {role}")
    conn.commit()

    # 3. Scan
    if state.do_scan and state.workspace_links:
        from loci.ingest.pipeline import scan_workspace
        for ws_id in state.workspace_links:
            ws = ws_repo.get(ws_id)
            if not ws:
                continue
            with console.status(f"  Scanning [bold]{ws.slug}[/bold]…"):
                res = scan_workspace(conn, ws_id)
            console.print(
                f"[green]✓[/green]  Scanned [bold]{ws.slug}[/bold]  "
                f"{res.new_raw} new · {res.deduped} deduped · {res.skipped} skipped"
            )
            for err in res.errors[:3]:
                console.print(f"  [yellow]⚠[/yellow]  {err}")

    # 4. Kickoff
    if state.do_kickoff and state.project_id:
        with console.status("  Running kickoff…"):
            from loci.jobs import enqueue
            from loci.jobs.queue import get_job
            from loci.jobs.worker import run_once
            jid = enqueue(conn, kind="kickoff", project_id=state.project_id, payload={"n": 6})
            run_once(conn)
            job = get_job(conn, jid)
        status = job.get("status", "?") if job else "unknown"
        color = "green" if status == "done" else "yellow"
        console.print(f"[{color}]✓[/{color}]  Kickoff {status}")

    console.print()
    console.print(
        f"[bold green]All done![/bold green]  "
        f"Try: [cyan]loci draft {state.slug} \"your question\"[/cyan]"
    )
    console.print()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s.lower().strip())
    return re.sub(r"[\s_]+", "-", s).strip("-")


def _load_state(conn: sqlite3.Connection, proj_id: str) -> _State:
    from loci.graph import ProjectRepository
    from loci.graph.workspaces import WorkspaceRepository

    proj = ProjectRepository(conn).get(proj_id)
    if not proj:
        return _State()
    links = {
        ws.id: link.role
        for ws, link in WorkspaceRepository(conn).linked_workspaces(proj_id)
    }
    return _State(
        project_id=proj.id,
        slug=proj.slug,
        name=proj.name,
        profile_md=proj.profile_md,
        workspace_links=links,
    )


def _print_banner() -> None:
    console.print()
    console.rule("[bold cyan]loci[/bold cyan] — project manager")
    console.print()


def _show_projects_table(conn: sqlite3.Connection) -> None:
    from loci.graph import ProjectRepository

    projects = ProjectRepository(conn).list()
    if not projects:
        return
    t = Table("slug", "name", "last active", show_lines=False, box=None)
    for p in projects:
        t.add_row(
            f"[bold]{p.slug}[/bold]",
            p.name,
            (p.last_active_at or "—")[:10],
        )
    console.print(t)
    console.print()
