"""Tests for Phase 2: aspect-graph data structures.

Covers:
- aspect_edges table and AspectGraphRepository (walk_hierarchy, opposites, upsert)
- aspect_embeddings table and AspectEmbedRepository (store, get, cosine_match)
- PMI computation via _compute_pmi_edges
- cosine_match threshold filtering
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from loci.graph.aspects import AspectRepository
from loci.graph.aspect_graph import AspectGraphRepository
from loci.graph.aspect_embed import AspectEmbedRepository
from loci.graph.models import new_id, now_iso


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_resource(conn, project_id: str, title: str = "Test") -> str:
    """Insert a minimal node + project membership, return resource_id."""
    rid = new_id()
    conn.execute(
        "INSERT INTO nodes(id, kind, subkind, title, status) VALUES (?,?,?,?,?)",
        (rid, "raw", "md", title, "live"),
    )
    conn.execute(
        "INSERT INTO raw_nodes(id, content_hash, canonical_path, mime, size_bytes) VALUES (?,?,?,?,?)",
        (rid, rid, "/fake/" + rid, "text/plain", 0),
    )
    conn.execute(
        "INSERT INTO project_membership(project_id, node_id, role) VALUES (?,?,'included')",
        (project_id, rid),
    )
    return rid


def _ensure(conn, label: str, project_id=None) -> str:
    """Ensure an aspect exists and return its id."""
    repo = AspectRepository(conn)
    aspect = repo.ensure_aspect(label, source="user", project_id=project_id)
    return aspect.id


def _unit_vec(dim: int, seed: int) -> np.ndarray:
    """Return a reproducible unit-normalised float32 vector."""
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    return v / (np.linalg.norm(v) + 1e-12)


# ---------------------------------------------------------------------------
# 1. test_aspect_edges_insert_and_walk
# ---------------------------------------------------------------------------

def test_aspect_edges_insert_and_walk(conn, project):
    """parent_of edge: deep-learning → transformer; walk from transformer → deep-learning."""
    ag = AspectGraphRepository(conn)
    ar = AspectRepository(conn)

    dl_id = _ensure(conn, "deep-learning")
    tr_id = _ensure(conn, "transformer")

    # Insert edge: deep-learning is parent_of transformer
    ag.add_edge(dl_id, tr_id, edge_type="parent_of")

    # Walk hierarchy from transformer — should reach deep-learning (parent)
    walked = ag.walk_hierarchy(tr_id, edge_types=("parent_of",))
    assert dl_id in walked, f"Expected deep-learning ({dl_id}) in walked={walked}"


def test_aspect_edges_insert_and_walk_child_to_parent(conn, project):
    """walk_hierarchy follows src→dst direction for parent_of."""
    ag = AspectGraphRepository(conn)

    parent_id = _ensure(conn, "machine-learning")
    child_id = _ensure(conn, "deep-learning")
    grandchild_id = _ensure(conn, "transformer")

    ag.add_edge(parent_id, child_id, edge_type="parent_of")
    ag.add_edge(child_id, grandchild_id, edge_type="parent_of")

    # Walk from grandchild with depth=1 → only child
    walked_d1 = ag.walk_hierarchy(grandchild_id, edge_types=("parent_of",), depth=1)
    assert child_id in walked_d1
    assert parent_id not in walked_d1

    # Walk with depth=2 → both parent and child
    walked_d2 = ag.walk_hierarchy(grandchild_id, edge_types=("parent_of",), depth=2)
    assert child_id in walked_d2
    assert parent_id in walked_d2

    # Start node excluded
    assert grandchild_id not in walked_d2


# ---------------------------------------------------------------------------
# 2. test_aspect_edges_upsert_idempotent
# ---------------------------------------------------------------------------

def test_aspect_edges_upsert_idempotent(conn, project):
    """Inserting same triple twice doesn't error; weight is updated on second call."""
    ag = AspectGraphRepository(conn)

    a_id = _ensure(conn, "supervised-learning")
    b_id = _ensure(conn, "classification")

    ag.add_edge(a_id, b_id, edge_type="related_to", weight=0.5)

    # Second insert with different weight — should update, not raise.
    ag.add_edge(a_id, b_id, edge_type="related_to", weight=0.9)

    # Verify exactly one row exists with updated weight.
    rows = conn.execute(
        "SELECT weight FROM aspect_edges WHERE src_aspect_id = ? AND dst_aspect_id = ? AND edge_type = 'related_to'",
        (a_id, b_id),
    ).fetchall()
    assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"
    assert abs(rows[0]["weight"] - 0.9) < 1e-6, f"Expected weight 0.9, got {rows[0]['weight']}"


def test_aspect_edges_upsert_project_scoped(conn, project):
    """Same triple with different project_id results in two separate rows."""
    ag = AspectGraphRepository(conn)

    a_id = _ensure(conn, "unsupervised")
    b_id = _ensure(conn, "clustering")

    ag.add_edge(a_id, b_id, edge_type="related_to", project_id=None)
    ag.add_edge(a_id, b_id, edge_type="related_to", project_id=project.id)

    rows = conn.execute(
        "SELECT project_id FROM aspect_edges WHERE src_aspect_id = ? AND dst_aspect_id = ?",
        (a_id, b_id),
    ).fetchall()
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# 3. test_opposites
# ---------------------------------------------------------------------------

def test_opposites(conn, project):
    """opposite_of edge: supervised → opposite_of → unsupervised; opposites() returns both sides."""
    ag = AspectGraphRepository(conn)

    sup_id = _ensure(conn, "supervised")
    unsup_id = _ensure(conn, "unsupervised")

    ag.add_edge(sup_id, unsup_id, edge_type="opposite_of")

    # Forward direction.
    opp_of_sup = ag.opposites(sup_id)
    assert unsup_id in opp_of_sup

    # Reverse direction: asking for opposites of unsupervised should find supervised.
    opp_of_unsup = ag.opposites(unsup_id)
    assert sup_id in opp_of_unsup


def test_opposites_project_filter(conn, project):
    """Project-scoped opposite edges are visible with project_id filter."""
    ag = AspectGraphRepository(conn)

    a_id = _ensure(conn, "high-precision")
    b_id = _ensure(conn, "high-recall")

    # Global edge.
    ag.add_edge(a_id, b_id, edge_type="opposite_of", project_id=None)

    # Query with project_id — global edges (project_id IS NULL) are included.
    result = ag.opposites(a_id, project_id=project.id)
    assert b_id in result

    # Query without project_id — global edges visible too.
    result_no_proj = ag.opposites(a_id, project_id=None)
    assert b_id in result_no_proj


# ---------------------------------------------------------------------------
# 4. test_pmi_computation
# ---------------------------------------------------------------------------

def test_pmi_computation(conn, project):
    """PMI edges created for aspects that co-occur frequently enough."""
    from loci.jobs.refresh_project_edges import _compute_pmi_edges

    ar = AspectRepository(conn)

    # Create 5 resources; 3 of them share both "nlp" and "transformer".
    # Only 1 has "unrelated". This should yield a positive PMI for (nlp, transformer).
    r1 = _insert_resource(conn, project.id, "Paper A")
    r2 = _insert_resource(conn, project.id, "Paper B")
    r3 = _insert_resource(conn, project.id, "Paper C")
    r4 = _insert_resource(conn, project.id, "Paper D")  # only nlp
    r5 = _insert_resource(conn, project.id, "Paper E")  # only unrelated

    ar.tag_resource(r1, ["nlp", "transformer"], project_id=project.id, source="user")
    ar.tag_resource(r2, ["nlp", "transformer"], project_id=project.id, source="user")
    ar.tag_resource(r3, ["nlp", "transformer"], project_id=project.id, source="user")
    ar.tag_resource(r4, ["nlp"], project_id=project.id, source="user")
    ar.tag_resource(r5, ["unrelated"], project_id=project.id, source="user")

    written = _compute_pmi_edges(conn, project.id)
    assert written > 0, "Expected at least one PMI edge"

    # Verify at least one co_aspect_pmi edge with positive weight.
    pmi_rows = conn.execute(
        "SELECT weight FROM aspect_edges WHERE edge_type = 'co_aspect_pmi' AND project_id = ?",
        (project.id,),
    ).fetchall()
    assert pmi_rows, "No co_aspect_pmi rows found"
    assert all(r["weight"] > 0 for r in pmi_rows), "All PMI weights should be positive"


def test_pmi_computation_skips_if_no_table(conn, project):
    """_compute_pmi_edges gracefully returns 0 when aspect_edges doesn't exist."""
    from loci.jobs.refresh_project_edges import _compute_pmi_edges

    # Drop the table to simulate un-migrated state.
    conn.execute("DROP TABLE IF EXISTS aspect_edges")

    result = _compute_pmi_edges(conn, project.id)
    assert result == 0


def test_pmi_requires_min_cooccurrence(conn, project):
    """Pairs that co-occur only once (< 2) must not produce PMI edges."""
    from loci.jobs.refresh_project_edges import _compute_pmi_edges

    ar = AspectRepository(conn)

    r1 = _insert_resource(conn, project.id, "Singleton Paper")
    ar.tag_resource(r1, ["aspect-alpha", "aspect-beta"], project_id=project.id, source="user")

    # Only one document has both — count_ab = 1, which is below threshold.
    written = _compute_pmi_edges(conn, project.id)
    assert written == 0, f"Expected 0 edges for single co-occurrence, got {written}"


# ---------------------------------------------------------------------------
# 5. test_cosine_match_threshold
# ---------------------------------------------------------------------------

def test_cosine_match_threshold(conn, project):
    """cosine_match returns aspect near query but not one below threshold."""
    ae = AspectEmbedRepository(conn)
    ar = AspectRepository(conn)

    dim = 4
    # Two aspects with known vectors.
    near_id = _ensure(conn, "neural-nets")
    far_id = _ensure(conn, "database-indexing")

    # near_vec is very close to the query.
    query_vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    near_vec = np.array([0.99, 0.14, 0.0, 0.0], dtype=np.float32)
    near_vec /= np.linalg.norm(near_vec)

    # far_vec is nearly orthogonal to query (low cosine).
    far_vec = np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)

    ae.store(near_id, near_vec, model_id="test-model")
    ae.store(far_id, far_vec, model_id="test-model")

    # With threshold 0.5: near should appear, far should not (cos ≈ 0).
    results = ae.cosine_match(query_vec, project_id=None, top_k=10, threshold=0.5)
    labels = [label for label, _score in results]
    assert "neural-nets" in labels, f"Expected neural-nets in results: {results}"
    assert "database-indexing" not in labels, f"Expected database-indexing NOT in results: {results}"


def test_cosine_match_returns_top_k(conn, project):
    """cosine_match respects top_k limit."""
    ae = AspectEmbedRepository(conn)

    dim = 4
    query_vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)

    for i in range(10):
        aid = _ensure(conn, f"aspect-{i:03d}")
        v = np.array([0.9, 0.1 * i, 0.0, 0.0], dtype=np.float32)
        v /= np.linalg.norm(v) + 1e-12
        ae.store(aid, v, model_id="test-model")

    results = ae.cosine_match(query_vec, project_id=None, top_k=3, threshold=0.0)
    assert len(results) <= 3


def test_cosine_match_empty_when_no_embeddings(conn, project):
    """cosine_match returns [] when no embeddings are stored."""
    ae = AspectEmbedRepository(conn)
    query_vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    results = ae.cosine_match(query_vec, project_id=None)
    assert results == []


def test_aspect_embed_store_and_get(conn, project):
    """store() then get() round-trips the embedding."""
    ae = AspectEmbedRepository(conn)
    aid = _ensure(conn, "autoencoder")
    vec = _unit_vec(8, seed=42)
    ae.store(aid, vec, model_id="test-model")

    retrieved = ae.get(aid)
    assert retrieved is not None
    assert retrieved.shape == vec.shape
    assert np.allclose(retrieved, vec, atol=1e-6)


def test_aspect_embed_upsert(conn, project):
    """Second store() call updates the embedding (upsert semantics)."""
    ae = AspectEmbedRepository(conn)
    aid = _ensure(conn, "variational-autoencoder")

    v1 = _unit_vec(8, seed=1)
    v2 = _unit_vec(8, seed=2)

    ae.store(aid, v1, model_id="m1")
    ae.store(aid, v2, model_id="m2")

    retrieved = ae.get(aid)
    assert np.allclose(retrieved, v2, atol=1e-6), "Second store should overwrite first"
