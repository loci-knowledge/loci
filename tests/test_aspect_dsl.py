"""Tests for the CNL aspect proposition DSL (aspect_dsl.py)."""

import pytest

from loci.graph.aspect_dsl import (
    ROLES,
    Proposition,
    parse,
    render,
    role_edge_det_id,
    to_aspect_edges,
)
from loci.retrieve.query_cnl import Query, parse_query, split_query


# ---------------------------------------------------------------------------
# Parser round-trip tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        # Flat label (degenerate proposition)
        ("literate-programming", Proposition(topic="literate-programming")),
        # topic + kind
        ("literate-programming as methodology", Proposition(topic="literate-programming", kind="methodology")),
        # topic + kind + role + target
        (
            "reproducibility as critique critiques frequentist-statistics",
            Proposition(topic="reproducibility", kind="critique", role="critiques", target="frequentist-statistics"),
        ),
        # topic + role + target (no kind)
        (
            "prequential-validation extends cross-validation",
            Proposition(topic="prequential-validation", role="extends", target="cross-validation"),
        ),
        # topic + kind + modifiers (no role)
        (
            "dropout as technique applies_to=overfitting scope=narrow",
            Proposition(topic="dropout", kind="technique", modifiers={"applies_to": "overfitting", "scope": "narrow"}),
        ),
        # topic + kind + role + target
        (
            "attention-is-all-you-need as reference introduces transformer",
            Proposition(topic="attention-is-all-you-need", kind="reference", role="introduces", target="transformer"),
        ),
    ],
)
def test_parse_examples(text: str, expected: Proposition) -> None:
    assert parse(text) == expected


def test_render_round_trip() -> None:
    examples = [
        "literate-programming",
        "literate-programming as methodology",
        "reproducibility as critique critiques frequentist-statistics",
        "prequential-validation extends cross-validation",
        "attention-is-all-you-need as reference introduces transformer",
    ]
    for s in examples:
        p = parse(s)
        assert parse(render(p)) == p, f"round-trip failed for {s!r}"


def test_render_flat_is_topic_only() -> None:
    p = Proposition(topic="reproducibility")
    assert render(p) == "reproducibility"


def test_render_full_proposition() -> None:
    p = Proposition(topic="reproducibility", kind="critique", role="critiques", target="frequentist-statistics")
    assert render(p) == "reproducibility as critique critiques frequentist-statistics"


# ---------------------------------------------------------------------------
# Degradation tests
# ---------------------------------------------------------------------------


def test_unknown_role_degrades_to_flat() -> None:
    # "zaps" is not a valid role — role should be dropped, treated as extra token
    p = parse("reproducibility zaps something")
    # "zaps" is not in ROLES, so no role is extracted; "something" treated as unknown token
    assert p.role is None
    assert p.topic == "reproducibility"


def test_role_without_target_degrades() -> None:
    # role present but next token is a modifier → role dropped
    p = parse("reproducibility as methodology critiques")
    # "critiques" at the end with no following slug → role dropped
    assert p.role is None


def test_unknown_kind_accepted_as_project_kind() -> None:
    # Non-global kinds should still parse (extensible)
    p = parse("dropout as custom-framework")
    assert p.kind == "custom-framework"
    assert p.topic == "dropout"


def test_empty_input_returns_unknown() -> None:
    p = parse("")
    assert p.topic == "unknown"


def test_parse_failure_degrades_gracefully() -> None:
    # Completely malformed: just spaces
    p = parse("   ")
    assert p.topic == "unknown"


# ---------------------------------------------------------------------------
# is_flat property
# ---------------------------------------------------------------------------


def test_is_flat_true_for_topic_only() -> None:
    p = Proposition(topic="foo")
    assert p.is_flat


def test_is_flat_false_with_kind() -> None:
    p = Proposition(topic="foo", kind="methodology")
    assert not p.is_flat


def test_is_flat_false_with_role() -> None:
    p = Proposition(topic="foo", role="critiques", target="bar")
    assert not p.is_flat


# ---------------------------------------------------------------------------
# to_aspect_edges tests
# ---------------------------------------------------------------------------


def test_to_aspect_edges_empty_for_flat() -> None:
    p = Proposition(topic="reproducibility")
    edges = to_aspect_edges(p, project_id="P", resource_id="R", aspect_id="A")
    assert edges == []


def test_to_aspect_edges_opposite_for_critiques() -> None:
    p = Proposition(topic="reproducibility", kind="critique", role="critiques", target="frequentist-statistics")
    edges = to_aspect_edges(p, project_id="P", resource_id="R", aspect_id="A")
    assert len(edges) == 1
    e = edges[0]
    assert e.edge_type == "opposite_of"
    assert e.src_topic == "reproducibility"
    assert e.dst_topic == "frequentist-statistics"
    assert e.weight == 1.0


def test_to_aspect_edges_parent_for_extends() -> None:
    p = Proposition(topic="prequential-validation", role="extends", target="cross-validation")
    edges = to_aspect_edges(p, project_id="P", resource_id="R", aspect_id="A")
    assert len(edges) == 1
    e = edges[0]
    assert e.edge_type == "parent_of"
    # For extends: target is parent of topic → src=target, dst=topic
    assert e.src_topic == "cross-validation"
    assert e.dst_topic == "prequential-validation"


def test_to_aspect_edges_related_for_supports() -> None:
    p = Proposition(topic="bayesian-inference", role="supports", target="causal-modeling")
    edges = to_aspect_edges(p, project_id="P", resource_id="R", aspect_id="A")
    e = edges[0]
    assert e.edge_type == "related_to"
    assert e.src_topic == "bayesian-inference"
    assert e.dst_topic == "causal-modeling"


def test_to_aspect_edges_deterministic_id() -> None:
    p = Proposition(topic="foo", role="critiques", target="bar")
    e1 = to_aspect_edges(p, project_id="P", resource_id="R", aspect_id="A")[0]
    e2 = to_aspect_edges(p, project_id="P", resource_id="R", aspect_id="A")[0]
    assert e1.deterministic_id == e2.deterministic_id

    # Different resource → different id
    e3 = to_aspect_edges(p, project_id="P", resource_id="R2", aspect_id="A")[0]
    assert e1.deterministic_id != e3.deterministic_id


def test_role_edge_det_id_matches_to_aspect_edges() -> None:
    p = Proposition(topic="foo", role="critiques", target="bar")
    edges = to_aspect_edges(p, project_id="P", resource_id="R", aspect_id="A")
    expected_id = role_edge_det_id("P", "R", "A", "critiques")
    assert edges[0].deterministic_id == expected_id


# ---------------------------------------------------------------------------
# Query parser tests
# ---------------------------------------------------------------------------


def test_parse_query_empty() -> None:
    q = parse_query("")
    assert q.is_empty


def test_parse_query_role_exact() -> None:
    q = parse_query("?role=critiques")
    assert q.role == "critiques"
    assert q.kind is None


def test_parse_query_kind_role_together() -> None:
    q = parse_query("?kind=methodology ?role=extends")
    assert q.kind == "methodology"
    assert q.role == "extends"


def test_parse_query_topic_fuzzy() -> None:
    q = parse_query("?topic~reproducibility")
    assert q.topic == "reproducibility"
    assert q.topic_fuzzy is True


def test_parse_query_target_fuzzy() -> None:
    q = parse_query("?target~bayesian")
    assert q.target == "bayesian"
    assert q.target_fuzzy is True


def test_split_query_separates_free_text() -> None:
    free, q = split_query("?role=critiques reproducibility frequentist")
    assert free == "reproducibility frequentist"
    assert q.role == "critiques"


def test_split_query_no_clauses() -> None:
    free, q = split_query("attention mechanism")
    assert free == "attention mechanism"
    assert q.is_empty


def test_split_query_only_clauses() -> None:
    free, q = split_query("?kind=methodology ?role=extends")
    assert free == ""
    assert q.kind == "methodology"
    assert q.role == "extends"


def test_query_to_sql_params_exact_topic() -> None:
    q = parse_query("?topic=reproducibility ?kind=methodology")
    params = q.to_sql_params()
    assert params["q_topic_pat"] == "reproducibility"
    assert params["q_kind"] == "methodology"
    assert params["q_role"] is None


def test_query_to_sql_params_fuzzy_topic() -> None:
    q = parse_query("?topic~repro")
    params = q.to_sql_params()
    assert params["q_topic_pat"] == "%repro%"
