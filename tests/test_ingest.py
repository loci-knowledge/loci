"""Ingest pipeline tests (use fake embedder so we don't load torch)."""

from __future__ import annotations

from loci.graph import Project, ProjectRepository
from loci.ingest import scan_path


def test_scan_one_directory(conn, fake_embedder, project, corpus_dir):
    res = scan_path(conn, project.id, corpus_dir, embedder=fake_embedder)
    assert res.scanned == 3
    assert res.new_raw == 3
    assert res.deduped == 0
    assert res.members_added == 3
    assert not res.errors


def test_scan_is_idempotent(conn, fake_embedder, project, corpus_dir):
    scan_path(conn, project.id, corpus_dir, embedder=fake_embedder)
    res2 = scan_path(conn, project.id, corpus_dir, embedder=fake_embedder)
    assert res2.scanned == 3
    assert res2.new_raw == 0
    assert res2.deduped == 3


def test_scan_dedups_across_projects(conn, fake_embedder, project, corpus_dir):
    scan_path(conn, project.id, corpus_dir, embedder=fake_embedder)
    p2 = ProjectRepository(conn).create(Project(slug="p2", name="P2"))
    res = scan_path(conn, p2.id, corpus_dir, embedder=fake_embedder)
    assert res.scanned == 3
    assert res.new_raw == 0  # all files already in raw_nodes
    assert res.members_added == 3  # but new memberships in p2

    # one raw_node per content_hash
    raw_count = conn.execute("SELECT COUNT(*) FROM raw_nodes").fetchone()[0]
    assert raw_count == 3


def test_scan_skips_unsupported(conn, fake_embedder, project, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "real.md").write_text("# Real\nhi")
    (src / "huge.bin").write_bytes(b"x" * 100)
    (src / "image.png").write_bytes(b"\x89PNG")
    res = scan_path(conn, project.id, src, embedder=fake_embedder)
    assert res.scanned == 1  # only real.md is in include_exts
    assert res.new_raw == 1


def test_dotdir_skipped(conn, fake_embedder, project, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "ok.md").write_text("# Ok\nhi")
    git = src / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main")
    res = scan_path(conn, project.id, src, embedder=fake_embedder)
    assert res.scanned == 1
