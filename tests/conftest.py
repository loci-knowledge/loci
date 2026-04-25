"""Shared pytest fixtures.

Each test gets its own fresh `LOCI_DATA_DIR` so DB state doesn't leak. The
embedder is loaded once per session because importing torch + downloading the
model is too slow to repeat per test (~20s cold).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from loci.config import get_settings


@pytest.fixture
def loci_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test data directory + cleared settings cache."""
    monkeypatch.setenv("LOCI_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture
def conn(loci_dir: Path):
    """Fresh DB with migrations applied."""
    from loci.db import migrate
    from loci.db.connection import close_thread_connection, connect

    migrate()
    c = connect()
    try:
        yield c
    finally:
        c.close()
        close_thread_connection()


@pytest.fixture
def fake_embedder(monkeypatch: pytest.MonkeyPatch):
    """A deterministic fake embedder so tests don't load the real model.

    Returns hashed-then-normalized vectors. Two identical strings produce
    identical vectors; different strings produce different vectors that are
    *not* meaningfully near in cosine space — enough for FTS+structure tests
    that don't care about semantic ranking.
    """
    import hashlib

    from loci.embed import local as embed_mod

    class FakeEmbedder:
        dim = 384
        batch_size = 32

        def encode(self, text: str) -> np.ndarray:
            return self.encode_batch([text])[0]

        def encode_batch(self, texts: list[str]) -> np.ndarray:
            out = np.zeros((len(texts), self.dim), dtype=np.float32)
            for i, t in enumerate(texts):
                seed = int.from_bytes(hashlib.sha256(t.encode()).digest()[:4], "little")
                rng = np.random.default_rng(seed)
                v = rng.standard_normal(self.dim).astype(np.float32)
                out[i] = v / (np.linalg.norm(v) + 1e-12)
            return out

    fake = FakeEmbedder()
    monkeypatch.setattr(embed_mod, "_INSTANCE", fake)
    monkeypatch.setattr(embed_mod, "get_embedder", lambda: fake)
    yield fake
    embed_mod.reset_embedder()


@pytest.fixture
def project(conn):
    """Create a test project, return it."""
    from loci.graph import Project, ProjectRepository
    return ProjectRepository(conn).create(Project(slug="test-proj", name="Test"))


@pytest.fixture
def corpus_dir(tmp_path: Path) -> Path:
    """Build a tiny on-disk corpus of markdown files."""
    src = tmp_path / "corpus"
    src.mkdir()
    (src / "rotary.md").write_text(
        "# Rotary Embeddings\n\nRoFormer encodes positions by rotating "
        "QK projections. Position is fused into the projection."
    )
    (src / "cross.md").write_text(
        "# Cross Attention\n\nCross attention reads keys/values from a "
        "different sequence than the query. Encoder-decoder."
    )
    (src / "sin.md").write_text(
        "# Sinusoidal Positions\n\nThe original Transformer adds sinusoidal "
        "embeddings to inputs before the first attention layer."
    )
    return src
