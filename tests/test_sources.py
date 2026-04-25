"""Tests for source-root registration and multi-root scanning."""

from __future__ import annotations

from loci.graph import SourceRepository
from loci.graph.models import Workspace
from loci.graph.workspaces import WorkspaceRepository
from loci.ingest import scan_workspace
from loci.ingest.pipeline import scan_project


def test_register_and_list(conn, project, tmp_path):
    src_dir = tmp_path / "papers"
    src_dir.mkdir()
    repo = SourceRepository(conn)
    src = repo.add(project.id, src_dir, label="papers")
    assert src.root_path == str(src_dir.resolve())
    assert src.label == "papers"
    listed = repo.list(project.id)
    assert len(listed) == 1
    assert listed[0].id == src.id


def test_register_idempotent_updates_label(conn, project, tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    repo = SourceRepository(conn)
    s1 = repo.add(project.id, src_dir, label="first")
    s2 = repo.add(project.id, src_dir, label="renamed")
    assert s1.id == s2.id
    assert repo.list(project.id)[0].label == "renamed"


def test_remove_by_id_or_path(conn, project, tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    repo = SourceRepository(conn)
    s = repo.add(project.id, src_dir)
    assert repo.remove(project.id, s.id) is True
    assert repo.remove(project.id, s.id) is False
    repo.add(project.id, src_dir)
    assert repo.remove(project.id, str(src_dir)) is True
    assert repo.list(project.id) == []


def test_scan_registered_walks_all_roots(conn, fake_embedder, project, workspace, tmp_path):
    """Workspace-based multi-root scanning: add sources to a workspace, scan them."""
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    (a / "alpha.md").write_text("# Alpha\nstuff about alpha")
    (b / "beta.md").write_text("# Beta\nstuff about beta")

    ws_repo = WorkspaceRepository(conn)
    ws_repo.add_source(workspace.id, a, label="a")
    ws_repo.add_source(workspace.id, b, label="b")

    res = scan_workspace(conn, workspace.id, embedder=fake_embedder)
    assert res.scanned == 2
    assert res.new_raw == 2
    # Both sources marked as scanned
    sources = ws_repo.list_sources(workspace.id)
    assert all(s.last_scanned_at is not None for s in sources)


def test_scan_project_walks_linked_workspaces(conn, fake_embedder, project, workspace, tmp_path):
    """scan_project aggregates scans across all linked workspaces."""
    a = tmp_path / "a"
    a.mkdir()
    (a / "paper.md").write_text("# A Paper\nsome content")

    ws_repo = WorkspaceRepository(conn)
    ws_repo.add_source(workspace.id, a)

    res = scan_project(conn, project.id, embedder=fake_embedder)
    assert res.new_raw == 1


def test_scan_handles_missing_root(conn, fake_embedder, workspace):
    """A missing root path is reported as an error, not a crash."""
    from loci.graph.models import new_id
    conn.execute(
        "INSERT INTO workspace_sources(id, workspace_id, root_path) VALUES (?, ?, '/nonexistent/path/xyz')",
        (new_id(), workspace.id),
    )
    res = scan_workspace(conn, workspace.id, embedder=fake_embedder)
    assert res.scanned == 0
    assert any("missing source root" in e for e in res.errors)
