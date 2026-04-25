"""Tests for source-root registration and multi-root scanning."""

from __future__ import annotations

from loci.graph import SourceRepository
from loci.ingest import scan_registered_sources


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


def test_scan_registered_walks_all_roots(conn, fake_embedder, project, tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    (a / "alpha.md").write_text("# Alpha\nstuff about alpha")
    (b / "beta.md").write_text("# Beta\nstuff about beta")
    SourceRepository(conn).add(project.id, a, label="a")
    SourceRepository(conn).add(project.id, b, label="b")
    res = scan_registered_sources(conn, project.id, embedder=fake_embedder)
    assert res.scanned == 2
    assert res.new_raw == 2
    # Both sources marked as scanned
    listed = SourceRepository(conn).list(project.id)
    assert all(s.last_scanned_at is not None for s in listed)


def test_scan_registered_handles_missing_root(conn, fake_embedder, project, tmp_path):
    SourceRepository(conn).add(project.id, tmp_path / "nope")
    res = scan_registered_sources(conn, project.id, embedder=fake_embedder)
    assert res.scanned == 0
    assert any("missing source root" in e for e in res.errors)
