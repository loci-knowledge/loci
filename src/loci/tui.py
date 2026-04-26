"""Textual TUI wizard for loci project setup and management.

Launched by `loci project create <slug>` (and `loci project manage`) when
stdin is a terminal. Provides full back/forward navigation, edit existing
projects, delete, and inline workspace creation.

Screens
-------
HomeScreen            list all projects; new / edit / delete
ProjectFormScreen     step 1 – name, slug, profile
WorkspaceScreen       step 2 – link/unlink workspaces with roles
ReviewScreen          step 3 – summary + scan/kickoff toggles
RunScreen             step 4 – live progress log
ConfirmDeleteModal    overlay – confirm project deletion
WorkspaceCreateModal  overlay – create a new workspace inline
"""
from __future__ import annotations

import re
import sqlite3
import traceback
from dataclasses import dataclass, field

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, ScrollableContainer
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Select,
    Static,
    TextArea,
)


# ─────────────────────────────────────────────────────────────────────────────
# Shared state
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class WizardState:
    project_id: str | None = None      # None → create new; set → edit
    project_slug: str = ""
    project_name: str = ""
    profile_md: str = ""
    workspace_links: dict[str, str] = field(default_factory=dict)  # ws_id → role
    scan: bool = True
    kickoff: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────────────────────────────────────

APP_CSS = """
Screen {
    layers: base overlay;
}

/* ── Layout globals ── */
#step-label {
    text-style: bold;
    color: $accent;
    padding: 1 2 0 2;
    height: 2;
}

.field-label {
    padding: 1 2 0 2;
    color: $text-muted;
    height: 2;
}

Input, Select {
    margin: 0 2;
}

TextArea {
    margin: 0 2;
    height: 10;
}

/* ── Navigation bar ── */
#nav-buttons {
    dock: bottom;
    height: 5;
    align: center middle;
    padding: 1 2;
    background: $surface-darken-1;
}

#nav-buttons Button {
    margin: 0 1;
    min-width: 18;
}

/* ── Home screen ── */
#home-title {
    text-style: bold;
    color: $accent;
    text-align: center;
    padding: 1 2;
    height: 3;
    border-bottom: solid $accent;
}

#new-project-btn {
    margin: 1 2;
}

.project-row {
    height: 3;
    layout: horizontal;
    padding: 0 2;
}

.project-row:hover {
    background: $surface-lighten-1;
}

.project-row .proj-label {
    width: 1fr;
    height: 3;
    content-align: left middle;
}

.project-row .row-actions {
    width: auto;
    layout: horizontal;
    height: 3;
}

.project-row Button {
    min-width: 10;
    height: 3;
    margin: 0 0 0 1;
}

#home-empty {
    text-align: center;
    color: $text-muted;
    padding: 4 2;
}

/* ── Workspace rows ── */
.ws-row {
    height: 3;
    layout: horizontal;
    padding: 0 2;
}

.ws-row:hover {
    background: $surface-lighten-1;
}

.ws-row Checkbox {
    width: 5;
    height: 3;
}

.ws-row .ws-label {
    width: 1fr;
    height: 3;
    content-align: left middle;
}

.ws-row Select {
    width: 18;
    margin: 0;
    height: 3;
}

#new-ws-btn {
    margin: 1 2;
}

/* ── Review screen ── */
#review-body {
    padding: 1 2;
}

/* ── Run screen ── */
#run-log {
    margin: 0 2;
    height: 1fr;
    border: solid $surface-darken-1;
}

/* ── Confirm delete modal ── */
ConfirmDeleteModal {
    align: center middle;
}

#confirm-dialog {
    background: $surface;
    border: thick $error;
    padding: 2 4;
    width: 60;
    height: auto;
}

#confirm-dialog Label {
    text-align: center;
    padding: 0 0 1 0;
    width: 100%;
}

#confirm-btns {
    layout: horizontal;
    align: center middle;
    height: 4;
}

#confirm-btns Button {
    margin: 0 1;
    min-width: 14;
}

/* ── Workspace create modal ── */
WorkspaceCreateModal {
    align: center middle;
}

#create-ws-dialog {
    background: $surface;
    border: thick $accent;
    padding: 2 4;
    width: 70;
    height: auto;
}

#create-ws-dialog .field-label {
    padding: 1 0 0 0;
}

#create-ws-dialog Input,
#create-ws-dialog Select {
    margin: 0;
    width: 100%;
}

#ws-create-btns {
    layout: horizontal;
    align: center middle;
    height: 4;
}

#ws-create-btns Button {
    margin: 0 1;
    min-width: 14;
}
"""


# ─────────────────────────────────────────────────────────────────────────────
# Compound widgets
# ─────────────────────────────────────────────────────────────────────────────

class ProjectRow(Static):
    """Single project row in the home screen."""

    def __init__(self, proj_id: str, slug: str, name: str) -> None:
        super().__init__("")
        self.proj_id = proj_id
        self.add_class("project-row")
        self._slug = slug
        self._name = name

    def compose(self) -> ComposeResult:
        yield Label(
            f"[bold]{self._slug}[/bold]  [dim]{self._name}[/dim]",
            classes="proj-label",
        )
        with Horizontal(classes="row-actions"):
            yield Button("Edit", id=f"edit-{self.proj_id}", variant="default")
            yield Button("Delete", id=f"del-{self.proj_id}", variant="error")


class WorkspaceRow(Static):
    """Checkbox + label + role select for a single workspace."""

    def __init__(
        self,
        ws_id: str,
        slug: str,
        name: str,
        kind: str,
        *,
        checked: bool = False,
        role: str = "primary",
    ) -> None:
        super().__init__("")
        self.ws_id = ws_id
        self.add_class("ws-row")
        self._slug = slug
        self._name = name
        self._kind = kind
        self._checked = checked
        self._role = role

    def compose(self) -> ComposeResult:
        yield Checkbox("", value=self._checked, id=f"cb-{self.ws_id}")
        yield Label(
            f"[bold]{self._slug}[/bold]  {self._name}  [dim]{self._kind}[/dim]",
            classes="ws-label",
        )
        yield Select(
            [("primary", "primary"), ("reference", "reference"), ("excluded", "excluded")],
            value=self._role,
            id=f"role-{self.ws_id}",
            disabled=not self._checked,
        )

    @property
    def is_checked(self) -> bool:
        return self.query_one(Checkbox).value

    @property
    def current_role(self) -> str:
        v = self.query_one(Select).value
        return str(v) if v != Select.BLANK else "primary"

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        self.query_one(Select).disabled = not event.value


# ─────────────────────────────────────────────────────────────────────────────
# Modal screens
# ─────────────────────────────────────────────────────────────────────────────

class ConfirmDeleteModal(ModalScreen[bool]):
    """'Are you sure?' overlay for project deletion."""

    def __init__(self, project_name: str) -> None:
        super().__init__()
        self._project_name = project_name

    def compose(self) -> ComposeResult:
        with Container(id="confirm-dialog"):
            yield Label(
                f"Delete project [bold]{self._project_name}[/bold]?",
                markup=True,
            )
            yield Label(
                "[dim]Removes the project and all its memberships.\n"
                "Raw nodes and workspaces are preserved.[/dim]",
                markup=True,
            )
            with Horizontal(id="confirm-btns"):
                yield Button("Cancel", id="cancel-del", variant="default")
                yield Button("Delete", id="confirm-del", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-del")


@dataclass
class _WsCreateResult:
    slug: str
    name: str
    kind: str
    source: str  # may be empty


class WorkspaceCreateModal(ModalScreen[_WsCreateResult | None]):
    """Inline form to create a new workspace."""

    def compose(self) -> ComposeResult:
        with Container(id="create-ws-dialog"):
            yield Label("[bold]Create workspace[/bold]", markup=True)
            yield Label("Slug", classes="field-label")
            yield Input(placeholder="my-sources", id="ws-slug")
            yield Label("Name", classes="field-label")
            yield Input(placeholder="My Sources", id="ws-name")
            yield Label("Kind", classes="field-label")
            yield Select(
                [
                    ("mixed", "mixed"),
                    ("papers", "papers"),
                    ("codebase", "codebase"),
                    ("notes", "notes"),
                    ("transcripts", "transcripts"),
                    ("web", "web"),
                ],
                value="mixed",
                id="ws-kind",
            )
            yield Label(
                "Source root path  [dim](optional — add more later with workspace add-source)[/dim]",
                classes="field-label",
                markup=True,
            )
            yield Input(placeholder="/path/to/files", id="ws-source")
            with Horizontal(id="ws-create-btns"):
                yield Button("Cancel", id="ws-cancel", variant="default")
                yield Button("Create", id="ws-create", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#ws-slug", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ws-cancel":
            self.dismiss(None)
        elif event.button.id == "ws-create":
            slug = self.query_one("#ws-slug", Input).value.strip()
            name = self.query_one("#ws-name", Input).value.strip()
            kind_val = self.query_one("#ws-kind", Select).value
            kind = str(kind_val) if kind_val != Select.BLANK else "mixed"
            source = self.query_one("#ws-source", Input).value.strip()
            if not slug:
                self.notify("Slug is required", severity="error")
                self.query_one("#ws-slug", Input).focus()
                return
            self.dismiss(_WsCreateResult(slug=slug, name=name or slug, kind=kind, source=source))


# ─────────────────────────────────────────────────────────────────────────────
# Main screens
# ─────────────────────────────────────────────────────────────────────────────

class HomeScreen(Screen):
    """Landing screen — lists all projects, offers new / edit / delete."""

    BINDINGS = [Binding("q", "quit", "Quit")]

    def __init__(self, conn: sqlite3.Connection, slug_hint: str = "") -> None:
        super().__init__()
        self.conn = conn
        self._slug_hint = slug_hint

    # ── Layout ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("loci — project manager", id="home-title")
        yield Button("+ New project", id="new-project-btn", variant="success")
        yield ScrollableContainer(id="project-list")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh()
        if self._slug_hint:
            from loci.graph import ProjectRepository
            existing = ProjectRepository(self.conn).get_by_slug(self._slug_hint)
            if existing is None:
                state = WizardState(
                    project_slug=self._slug_hint,
                    project_name=self._slug_hint,
                )
                self.app.push_screen(ProjectFormScreen(state, self.conn))
            else:
                self.notify(
                    f"'{self._slug_hint}' already exists — click Edit to modify it",
                    severity="warning",
                    timeout=6,
                )

    def on_screen_resume(self) -> None:
        """Refresh the list whenever we return from a child screen."""
        self._refresh()

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        from loci.graph import ProjectRepository
        projects = ProjectRepository(self.conn).list()
        container = self.query_one("#project-list", ScrollableContainer)
        container.remove_children()
        if not projects:
            container.mount(Static(
                "No projects yet.  Click [bold]+ New project[/bold] to get started.",
                id="home-empty",
                markup=True,
            ))
        else:
            for p in projects:
                container.mount(ProjectRow(p.id, p.slug, p.name))

    # ── Events ──────────────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn = event.button.id or ""
        if btn == "new-project-btn":
            self.app.push_screen(ProjectFormScreen(WizardState(), self.conn))
        elif btn.startswith("edit-"):
            self._open_edit(btn[5:])
        elif btn.startswith("del-"):
            self._open_delete(btn[4:])

    def _open_edit(self, proj_id: str) -> None:
        from loci.graph import ProjectRepository
        from loci.graph.workspaces import WorkspaceRepository
        proj = ProjectRepository(self.conn).get(proj_id)
        if not proj:
            return
        links = {
            ws.id: link.role
            for ws, link in WorkspaceRepository(self.conn).linked_workspaces(proj_id)
        }
        state = WizardState(
            project_id=proj.id,
            project_slug=proj.slug,
            project_name=proj.name,
            profile_md=proj.profile_md,
            workspace_links=links,
        )
        self.app.push_screen(ProjectFormScreen(state, self.conn))

    def _open_delete(self, proj_id: str) -> None:
        from loci.graph import ProjectRepository
        proj = ProjectRepository(self.conn).get(proj_id)
        if not proj:
            return

        def _after(confirmed: bool | None) -> None:
            if confirmed:
                from loci.graph import ProjectRepository
                ProjectRepository(self.conn).delete(proj_id)
                self.conn.commit()
                self.notify("Project deleted")
                self._refresh()

        self.app.push_screen(ConfirmDeleteModal(proj.name), _after)

    def action_quit(self) -> None:
        self.app.exit()


class ProjectFormScreen(Screen):
    """Step 1 — edit project name, slug, and profile markdown."""

    BINDINGS = [Binding("escape", "back", "Back")]

    def __init__(self, state: WizardState, conn: sqlite3.Connection) -> None:
        super().__init__()
        self.state = state
        self.conn = conn
        self._last_auto_slug = ""

    # ── Layout ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        action = "Edit" if self.state.project_id else "New"
        yield Header(show_clock=False)
        yield Static(f"Step 1 of 3  —  {action} project details", id="step-label")
        with ScrollableContainer():
            yield Label("Name", classes="field-label")
            yield Input(
                value=self.state.project_name,
                placeholder="My Research Project",
                id="proj-name",
            )
            yield Label("Slug  [dim](lowercase letters, numbers, hyphens)[/dim]", classes="field-label", markup=True)
            yield Input(
                value=self.state.project_slug,
                placeholder="my-research",
                id="proj-slug",
            )
            yield Label(
                "Profile  [dim](markdown; seeds kickoff — what you want from loci, 50–300 words)[/dim]",
                classes="field-label",
                markup=True,
            )
            yield TextArea(text=self.state.profile_md, id="proj-profile")
        with Horizontal(id="nav-buttons"):
            yield Button("← Back", id="back", variant="default")
            yield Button("Next →", id="next", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self._last_auto_slug = _slugify(self.state.project_name)
        self.query_one("#proj-name", Input).focus()

    # ── Events ──────────────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.app.pop_screen()
        elif event.button.id == "next":
            self._validate_and_advance()

    def action_back(self) -> None:
        self.app.pop_screen()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Auto-derive slug from the name field while the user hasn't edited it manually."""
        if event.input.id != "proj-name":
            return
        auto = _slugify(event.value)
        slug_input = self.query_one("#proj-slug", Input)
        if not slug_input.value or slug_input.value == self._last_auto_slug:
            slug_input.value = auto
            self._last_auto_slug = auto

    # ── Validation ──────────────────────────────────────────────────────────

    def _validate_and_advance(self) -> None:
        name = self.query_one("#proj-name", Input).value.strip()
        slug = self.query_one("#proj-slug", Input).value.strip()
        profile = self.query_one("#proj-profile", TextArea).text

        if not name:
            self.notify("Name is required", severity="error")
            self.query_one("#proj-name", Input).focus()
            return
        if not slug:
            self.notify("Slug is required", severity="error")
            self.query_one("#proj-slug", Input).focus()
            return
        if not re.match(r"^[a-z0-9][a-z0-9-]*$", slug):
            self.notify("Slug: lowercase letters, numbers, and hyphens only", severity="error")
            self.query_one("#proj-slug", Input).focus()
            return

        # Check slug collision
        from loci.graph import ProjectRepository
        existing = ProjectRepository(self.conn).get_by_slug(slug)
        if existing and existing.id != self.state.project_id:
            self.notify(
                f"Slug '{slug}' is already used by '{existing.name}' — choose another",
                severity="error",
            )
            self.query_one("#proj-slug", Input).focus()
            return

        self.state.project_name = name
        self.state.project_slug = slug
        self.state.profile_md = profile
        self.app.push_screen(WorkspaceScreen(self.state, self.conn))


class WorkspaceScreen(Screen):
    """Step 2 — choose which workspaces to link and with what role."""

    BINDINGS = [Binding("escape", "back", "Back")]

    def __init__(self, state: WizardState, conn: sqlite3.Connection) -> None:
        super().__init__()
        self.state = state
        self.conn = conn

    # ── Layout ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("Step 2 of 3  —  Link workspaces", id="step-label")
        with ScrollableContainer():
            yield Container(id="ws-rows")
            yield Button("+ Create new workspace", id="new-ws-btn", variant="default")
        with Horizontal(id="nav-buttons"):
            yield Button("← Back", id="back", variant="default")
            yield Button("Next →", id="next", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_rows()

    # ── Events ──────────────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.app.pop_screen()
        elif event.button.id == "next":
            self._collect_and_advance()
        elif event.button.id == "new-ws-btn":
            self.app.push_screen(WorkspaceCreateModal(), self._after_ws_created)

    def action_back(self) -> None:
        self.app.pop_screen()

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _refresh_rows(self) -> None:
        from loci.graph.workspaces import WorkspaceRepository
        workspaces = WorkspaceRepository(self.conn).list()
        container = self.query_one("#ws-rows", Container)
        container.remove_children()
        if not workspaces:
            container.mount(Static(
                "[dim]No workspaces yet — click '+ Create new workspace' below.[/dim]",
                markup=True,
            ))
            return
        for ws in workspaces:
            container.mount(WorkspaceRow(
                ws.id, ws.slug, ws.name, ws.kind,
                checked=ws.id in self.state.workspace_links,
                role=self.state.workspace_links.get(ws.id, "primary"),
            ))

    def _collect_and_advance(self) -> None:
        links: dict[str, str] = {}
        for row in self.query(WorkspaceRow):
            if row.is_checked:
                links[row.ws_id] = row.current_role
        self.state.workspace_links = links
        self.app.push_screen(ReviewScreen(self.state, self.conn))

    def _after_ws_created(self, result: _WsCreateResult | None) -> None:
        if result is None:
            return
        from pathlib import Path

        from loci.graph.models import Workspace, new_id, now_iso
        from loci.graph.workspaces import WorkspaceRepository
        ws_repo = WorkspaceRepository(self.conn)
        if ws_repo.get_by_slug(result.slug):
            self.notify(f"Workspace slug '{result.slug}' already exists", severity="error")
            return
        ws = Workspace(
            id=new_id(), slug=result.slug, name=result.name,
            description_md="", kind=result.kind,
            created_at=now_iso(), last_active_at=now_iso(),
        )
        ws_repo.create(ws)
        if result.source:
            ws_repo.add_source(ws.id, Path(result.source).expanduser())
        self.conn.commit()
        self.notify(f"Workspace '{result.slug}' created", severity="information")
        self._refresh_rows()


class ReviewScreen(Screen):
    """Step 3 — summary of changes + scan/kickoff toggles."""

    BINDINGS = [Binding("escape", "back", "Back")]

    def __init__(self, state: WizardState, conn: sqlite3.Connection) -> None:
        super().__init__()
        self.state = state
        self.conn = conn

    # ── Layout ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("Step 3 of 3  —  Review & apply", id="step-label")
        with ScrollableContainer():
            yield Static(self._summary_text(), id="review-body", markup=True)
            yield Checkbox("Scan linked workspaces after setup", value=True, id="do-scan")
            yield Checkbox(
                "Run kickoff to seed interpretation graph  [dim](requires scan)[/dim]",
                value=True,
                id="do-kickoff",
                markup=True,
            )
        with Horizontal(id="nav-buttons"):
            yield Button("← Back", id="back", variant="default")
            yield Button("✓  Apply", id="apply", variant="success")
        yield Footer()

    # ── Events ──────────────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.app.pop_screen()
        elif event.button.id == "apply":
            self.state.scan = self.query_one("#do-scan", Checkbox).value
            self.state.kickoff = self.query_one("#do-kickoff", Checkbox).value
            # switch_screen: no going back from the run screen
            self.app.switch_screen(RunScreen(self.state, self.conn))

    def action_back(self) -> None:
        self.app.pop_screen()

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _summary_text(self) -> str:
        from loci.graph.workspaces import WorkspaceRepository
        action = "Update" if self.state.project_id else "Create"
        lines = [
            f"[bold]{action} project:[/bold]  "
            f"{self.state.project_name}  [dim][{self.state.project_slug}][/dim]",
            "",
        ]
        if self.state.workspace_links:
            lines.append("[bold]Workspaces to link:[/bold]")
            ws_repo = WorkspaceRepository(self.conn)
            for ws_id, role in self.state.workspace_links.items():
                ws = ws_repo.get(ws_id)
                label = ws.slug if ws else ws_id
                lines.append(f"  • [bold]{label}[/bold]  [dim]({role})[/dim]")
        else:
            lines.append("[dim]No workspaces selected — you can link them later.[/dim]")
        lines.append("")
        return "\n".join(lines)


class RunScreen(Screen):
    """Step 4 — executes all operations in a background thread, streams output."""

    def __init__(self, state: WizardState, conn: sqlite3.Connection) -> None:
        super().__init__()
        self.state = state
        self.conn = conn

    # ── Layout ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("Applying…", id="step-label")
        yield RichLog(id="run-log", markup=True, highlight=True, auto_scroll=True)
        with Horizontal(id="nav-buttons"):
            yield Button("Done — exit", id="done", variant="primary", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        self._do_run()

    # ── Background worker ────────────────────────────────────────────────────

    @work(thread=True)
    def _do_run(self) -> None:
        log = self.query_one(RichLog)

        def emit(msg: str) -> None:
            self.call_from_thread(log.write, msg)

        try:
            self._apply(emit)
        except Exception as exc:
            emit(f"[red]✗  Unexpected error:[/red] {exc}")
            emit(f"[dim]{traceback.format_exc()}[/dim]")

        emit("\n[bold green]All done![/bold green]  Press Done to exit.")
        done_btn = self.query_one("#done", Button)
        self.call_from_thread(setattr, done_btn, "disabled", False)
        self.call_from_thread(done_btn.focus)

    def _apply(self, emit: callable) -> None:  # type: ignore[type-arg]
        from loci.graph import Project, ProjectRepository
        from loci.graph.workspaces import WorkspaceRepository

        conn = self.conn
        state = self.state
        proj_repo = ProjectRepository(conn)
        ws_repo = WorkspaceRepository(conn)

        # ── 1. Create or update project ──────────────────────────────────
        if state.project_id is None:
            proj = Project(
                slug=state.project_slug,
                name=state.project_name,
                profile_md=state.profile_md,
            )
            proj_repo.create(proj)
            conn.commit()
            state.project_id = proj.id
            emit(f"[green]✓[/green] Created project [bold]{proj.slug}[/bold]  [dim]{proj.id}[/dim]")
        else:
            proj_repo.update(
                state.project_id,
                state.project_slug,
                state.project_name,
                state.profile_md,
            )
            conn.commit()
            emit(f"[green]✓[/green] Updated project [bold]{state.project_slug}[/bold]")

        # ── 2. Sync workspace links ──────────────────────────────────────
        if state.project_id:
            existing_ids = {
                ws.id for ws, _ in ws_repo.linked_workspaces(state.project_id)
            }
            # Unlink removed ones (edit mode only)
            for ws_id in existing_ids - set(state.workspace_links):
                ws_repo.unlink_project(state.project_id, ws_id)
                ws = ws_repo.get(ws_id)
                emit(f"[yellow]−[/yellow]  Unlinked {ws.slug if ws else ws_id}")

            # Link new / update existing
            for ws_id, role in state.workspace_links.items():
                ws = ws_repo.get(ws_id)
                if ws:
                    ws_repo.link_project(state.project_id, ws_id, role=role)  # type: ignore[arg-type]
                    emit(f"[green]✓[/green] Linked [bold]{ws.slug}[/bold] as {role}")
            conn.commit()

        # ── 3. Scan ──────────────────────────────────────────────────────
        if state.scan and state.workspace_links:
            from loci.ingest.pipeline import scan_workspace
            for ws_id in state.workspace_links:
                ws = ws_repo.get(ws_id)
                if not ws:
                    continue
                emit(f"[dim]  Scanning {ws.slug}…[/dim]")
                res = scan_workspace(conn, ws_id)
                emit(
                    f"[green]✓[/green] Scanned [bold]{ws.slug}[/bold]  "
                    f"{res.new_raw} new  {res.deduped} deduped  {res.skipped} skipped"
                )
                for err in res.errors[:3]:
                    emit(f"  [yellow]⚠[/yellow]  {err}")

        # ── 4. Kickoff ────────────────────────────────────────────────────
        if state.kickoff and state.project_id:
            emit("[dim]  Running kickoff…[/dim]")
            from loci.jobs import enqueue
            from loci.jobs.queue import get_job
            from loci.jobs.worker import run_once
            jid = enqueue(conn, kind="kickoff", project_id=state.project_id, payload={"n": 6})
            run_once(conn)
            job = get_job(conn, jid)
            status = job.get("status", "?") if job else "unknown"
            color = "green" if status == "done" else "yellow"
            emit(f"[{color}]✓[/{color}]  Kickoff {status}")

    # ── Events ──────────────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "done":
            self.app.exit()


# ─────────────────────────────────────────────────────────────────────────────
# App entry point
# ─────────────────────────────────────────────────────────────────────────────

class ProjectWizardApp(App):
    CSS = APP_CSS
    TITLE = "loci"
    SUB_TITLE = "project manager"
    BINDINGS = [Binding("ctrl+c", "quit", "Quit", priority=True)]

    def __init__(self, conn: sqlite3.Connection, slug_hint: str = "") -> None:
        super().__init__()
        self.conn = conn
        self._slug_hint = slug_hint

    def on_mount(self) -> None:
        self.push_screen(HomeScreen(self.conn, self._slug_hint))

    def action_quit(self) -> None:
        self.exit()


def run_wizard(conn: sqlite3.Connection, slug_hint: str = "") -> None:
    """Launch the TUI wizard, blocking until the user exits."""
    ProjectWizardApp(conn, slug_hint).run()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s.lower().strip())
    return re.sub(r"[\s_]+", "-", s).strip("-")
