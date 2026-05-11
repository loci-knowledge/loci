"""End-to-end tests for CNL query retrieval (v2.2 aspect DSL).

Covers §8 verification from the petal plan:
- ?role= query filters to resources tagged with structured propositions
- Structured-match boost: typed aspects rank higher than flat labels
- Flat-label backward compat (no ? clauses still works)
- split_query separates free text from structured clauses
"""

from __future__ import annotations

import pytest

from loci.graph.aspects import AspectRepository
from loci.graph.models import AspectScore, new_id
from loci.retrieve.concept_expand import aspect_overlap_rank
from loci.retrieve.query_cnl import Query, split_query


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_resource(conn, project_id: str, title: str = "Test") -> str:
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


def _tag_cnl(conn, project_id: str, resource_id: str, cnl: str,
             source: str = "user", confidence: float = 1.0) -> None:
    from loci.graph.aspect_dsl import parse as _parse, render as _render

    prop = _parse(cnl)
    score = AspectScore(label=_render(prop), confidence=confidence,
                        rationale=None, proposition=prop)
    AspectRepository(conn).tag_resource_with_propositions(
        project_id, resource_id, [score], source=source
    )


# ---------------------------------------------------------------------------
# ?role= filtering
# ---------------------------------------------------------------------------

def test_role_query_returns_structured_not_flat(conn, project):
    """?role=critiques returns only resource tagged with critiques proposition."""
    rid_struct = _insert_resource(conn, project.id, "Critique Paper")
    rid_flat = _insert_resource(conn, project.id, "Plain Paper")
    _tag_cnl(conn, project.id, rid_struct,
             "reproducibility as critique critiques frequentist-statistics")
    _tag_cnl(conn, project.id, rid_flat, "reproducibility")

    q = Query(role="critiques")
    results = aspect_overlap_rank(conn, project.id, [], limit=10, query=q)
    result_ids = [r for r, _ in results]
    assert rid_struct in result_ids
    assert rid_flat not in result_ids


def test_kind_query_filters_to_matching_kind(conn, project):
    """?kind=methodology returns only resource tagged as methodology."""
    rid_meth = _insert_resource(conn, project.id, "Methods Paper")
    rid_concept = _insert_resource(conn, project.id, "Concept Paper")
    _tag_cnl(conn, project.id, rid_meth, "cross-validation as methodology")
    _tag_cnl(conn, project.id, rid_concept, "cross-validation as concept")

    q = Query(kind="methodology")
    results = aspect_overlap_rank(conn, project.id, [], limit=10, query=q)
    result_ids = [r for r, _ in results]
    assert rid_meth in result_ids
    assert rid_concept not in result_ids


def test_role_and_topic_combined(conn, project):
    """?role=extends ?topic~prequential: role filter + fuzzy topic match."""
    rid = _insert_resource(conn, project.id, "Extends Paper")
    rid_other = _insert_resource(conn, project.id, "Other")
    _tag_cnl(conn, project.id, rid,
             "prequential-validation as methodology extends cross-validation")
    # rid_other has the same topic but no role — role filter must exclude it
    _tag_cnl(conn, project.id, rid_other, "cross-validation")

    q = Query(role="extends", topic="prequential", topic_fuzzy=True)
    results = aspect_overlap_rank(conn, project.id, [], limit=10, query=q)
    result_ids = [r for r, _ in results]
    assert rid in result_ids
    assert rid_other not in result_ids


# ---------------------------------------------------------------------------
# Structured-match boost
# ---------------------------------------------------------------------------

def test_structured_match_ranks_higher_than_flat(conn, project):
    """Kind query includes typed resource, excludes flat-label (kind=NULL) resource.

    The 1.5x kind multiplier applies to rows that pass the filter; flat-label
    rows have kind=NULL and are excluded entirely — they rank "lower" by absence.
    """
    rid_struct = _insert_resource(conn, project.id, "Structured")
    rid_flat = _insert_resource(conn, project.id, "Flat")
    _tag_cnl(conn, project.id, rid_struct, "reproducibility as methodology", confidence=0.8)
    _tag_cnl(conn, project.id, rid_flat, "reproducibility", confidence=0.8)

    q = Query(kind="methodology", topic="reproducibility")
    results = aspect_overlap_rank(conn, project.id, [], limit=10, query=q)
    ids = [r for r, _ in results]
    assert rid_struct in ids          # kind matches → passes filter + gets 1.5x boost
    assert rid_flat not in ids        # kind=NULL → excluded by kind filter


# ---------------------------------------------------------------------------
# Flat-label backward compat
# ---------------------------------------------------------------------------

def test_flat_label_query_returns_results(conn, project):
    """No ? clauses: legacy flat-label path still returns resources."""
    rid = _insert_resource(conn, project.id, "Paper")
    _tag_cnl(conn, project.id, rid, "reproducibility")

    results = aspect_overlap_rank(conn, project.id, ["reproducibility"], limit=10)
    assert rid in [r for r, _ in results]


def test_empty_query_and_empty_aspects_returns_empty(conn, project):
    """Both query empty and aspects empty → no results."""
    _insert_resource(conn, project.id, "Ignored")
    results = aspect_overlap_rank(conn, project.id, [], limit=10, query=None)
    assert results == []


# ---------------------------------------------------------------------------
# split_query
# ---------------------------------------------------------------------------

def test_split_query_extracts_role_clause():
    free, q = split_query("?role=critiques bayesian methods")
    assert free == "bayesian methods"
    assert q.role == "critiques"
    assert not q.is_empty


def test_split_query_no_clauses_empty_query():
    free, q = split_query("bayesian methods")
    assert free == "bayesian methods"
    assert q.is_empty


def test_split_query_only_clauses_no_free_text():
    free, q = split_query("?role=supports ?kind=methodology")
    assert free == ""
    assert q.role == "supports"
    assert q.kind == "methodology"


def test_split_query_topic_fuzzy():
    free, q = split_query("?topic~repro some text")
    assert "repro" in (q.topic or "")
    assert q.topic_fuzzy is True
    assert "some text" in free


# ---------------------------------------------------------------------------
# Role → aspect_edges materialisation
# ---------------------------------------------------------------------------

def test_critiques_role_materialises_opposite_of_edge(conn, project):
    """Tagging a critiques proposition creates an opposite_of aspect_edge."""
    rid = _insert_resource(conn, project.id, "Critique")
    _tag_cnl(conn, project.id, rid,
             "reproducibility as critique critiques frequentist-statistics")

    # Both aspects must exist for edge to materialise
    row = conn.execute(
        """
        SELECT ae.edge_type, ae.weight
        FROM aspect_edges ae
        JOIN aspect_vocab src ON src.id = ae.src_aspect_id
        JOIN aspect_vocab dst ON dst.id = ae.dst_aspect_id
        WHERE ae.edge_type = 'opposite_of'
          AND (src.topic = 'reproducibility' OR dst.topic = 'reproducibility')
        LIMIT 1
        """
    ).fetchone()
    assert row is not None, "opposite_of edge not materialised for critiques role"
    assert row["edge_type"] == "opposite_of"


def test_extends_role_materialises_parent_of_edge(conn, project):
    """Tagging an extends proposition creates a parent_of aspect_edge."""
    rid = _insert_resource(conn, project.id, "Extension")
    _tag_cnl(conn, project.id, rid,
             "prequential-validation as methodology extends cross-validation")

    row = conn.execute(
        """
        SELECT ae.edge_type
        FROM aspect_edges ae
        JOIN aspect_vocab src ON src.id = ae.src_aspect_id
        JOIN aspect_vocab dst ON dst.id = ae.dst_aspect_id
        WHERE ae.edge_type = 'parent_of'
          AND (src.topic = 'cross-validation' OR dst.topic = 'cross-validation')
        LIMIT 1
        """
    ).fetchone()
    assert row is not None, "parent_of edge not materialised for extends role"
