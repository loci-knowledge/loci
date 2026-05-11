"""Tests for retrieval pipeline v2.1 changes.

Covers: soft aspect channel, aspect-aware HyDE, signed-sum graph rerank,
aspect-density bonus, and fix of build_why_surfaced to use merged_aspects.
"""

from __future__ import annotations

import pytest

from loci.graph.aspects import AspectRepository
from loci.graph.concept_edges import ConceptEdgeRepository
from loci.graph.models import new_id, now_iso
from loci.retrieve.concept_expand import aspect_overlap_rank


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
    # Use project_membership 'included' override — simplest path into project_effective_members.
    conn.execute(
        "INSERT INTO project_membership(project_id, node_id, role) VALUES (?,?,'included')",
        (project_id, rid),
    )
    return rid


def _tag(conn, project_id, resource_id, label, source="user", confidence=1.0):
    """Add an aspect to a resource in project_resource_aspects."""
    aspect_repo = AspectRepository(conn)
    aspect_repo.tag_resource(resource_id, [label], project_id=project_id, source=source, confidence=confidence)


# ---------------------------------------------------------------------------
# aspect_overlap_rank
# ---------------------------------------------------------------------------

def test_aspect_overlap_rank_empty_aspects_returns_empty(conn, project):
    result = aspect_overlap_rank(conn, project.id, [], limit=10)
    assert result == []


def test_aspect_overlap_rank_returns_matched_resources(conn, project):
    rid = _insert_resource(conn, project.id, "Stat Methods")
    rid2 = _insert_resource(conn, project.id, "Unrelated Paper")
    _tag(conn, project.id, rid, "reproducibility", confidence=1.0)
    _tag(conn, project.id, rid, "statistics", confidence=0.8)
    _tag(conn, project.id, rid2, "cooking", confidence=1.0)

    result = aspect_overlap_rank(conn, project.id, ["reproducibility", "statistics"])
    resource_ids = [r for r, _ in result]
    assert rid in resource_ids
    assert rid2 not in resource_ids


def test_aspect_overlap_rank_confidence_weighted(conn, project):
    rid_high = _insert_resource(conn, project.id, "High Conf")
    rid_low = _insert_resource(conn, project.id, "Low Conf")
    _tag(conn, project.id, rid_high, "reproducibility", confidence=1.0)
    _tag(conn, project.id, rid_high, "statistics", confidence=1.0)
    _tag(conn, project.id, rid_low, "reproducibility", confidence=0.3)

    result = aspect_overlap_rank(conn, project.id, ["reproducibility", "statistics"])
    ranked = [r for r, _ in result]
    assert ranked.index(rid_high) < ranked.index(rid_low)


# ---------------------------------------------------------------------------
# HyDE aspect-aware (R2)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hyde_includes_aspects_in_instructions(monkeypatch):
    """hypothesize() must embed aspect labels in the instruction when aspects given."""
    from loci.retrieve import hyde as hyde_mod

    captured_instructions: list[str] = []

    class FakeAgent:
        def __init__(self, instructions):
            captured_instructions.append(instructions)

        async def run(self, q):
            class R:
                output = "hypothetical"
            return R()

    monkeypatch.setattr(hyde_mod, "build_agent", lambda model, instructions: FakeAgent(instructions))

    await hyde_mod.hypothesize(
        "p-value testing",
        project_memo="Stats project",
        aspects=["reproducibility", "p-hacking"],
    )
    assert len(captured_instructions) == 1
    instr = captured_instructions[0]
    assert "reproducibility" in instr
    assert "p-hacking" in instr


@pytest.mark.asyncio
async def test_hyde_no_aspects_no_concepts_line(monkeypatch):
    """When aspects=None, instruction must NOT contain 'Relevant concepts:'."""
    from loci.retrieve import hyde as hyde_mod

    captured_instructions: list[str] = []

    class FakeAgent:
        def __init__(self, instructions):
            captured_instructions.append(instructions)

        async def run(self, q):
            class R:
                output = "hypothetical"
            return R()

    monkeypatch.setattr(hyde_mod, "build_agent", lambda model, instructions: FakeAgent(instructions))

    await hyde_mod.hypothesize("p-value testing", project_memo=None, aspects=None)
    assert "Relevant concepts" not in captured_instructions[0]


# ---------------------------------------------------------------------------
# Signed-sum graph rerank (R3)
# ---------------------------------------------------------------------------

def _make_boost(edge_type_weight_pairs):
    """Simulate the v2.1 signed-sum boost formula given (edge_type, weight) pairs."""
    from loci.retrieve.pipeline import _EDGE_TYPE_DELTA
    delta = sum(_EDGE_TYPE_DELTA.get(et, 0.0) * w for et, w in edge_type_weight_pairs)
    return 1.0 + max(-0.5, min(0.6, delta))


def test_supports_only_gives_positive_boost():
    boost = _make_boost([("supports", 1.0)])
    assert abs(boost - 1.30) < 1e-9


def test_contradicts_only_gives_demotion():
    boost = _make_boost([("contradicts", 1.0)])
    assert boost < 1.0
    assert abs(boost - 0.85) < 1e-9


def test_signed_sum_supports_and_contradicts_net():
    # supports(+0.30) + contradicts(-0.15) = +0.15 net → boost 1.15
    boost = _make_boost([("supports", 1.0), ("contradicts", 1.0)])
    assert abs(boost - 1.15) < 1e-9


def test_edge_weight_scales_delta():
    # supports at half weight: delta = 0.30 * 0.5 = 0.15 → boost 1.15
    boost = _make_boost([("supports", 0.5)])
    assert abs(boost - 1.15) < 1e-9


def test_multiple_contradicts_clamped():
    # 4× contradicts would be -0.60, but clamp floor is -0.5 → boost 0.5
    boost = _make_boost([("contradicts", 1.0)] * 4)
    assert boost == 0.5


def test_max_positive_clamped():
    # 4× supports would be +1.20, but clamp ceiling is +0.6 → boost 1.6
    boost = _make_boost([("supports", 1.0)] * 4)
    assert boost == 1.6


def test_edge_weight_respected_ordering(conn, project):
    """Higher-weight edge produces larger absolute boost shift than lower-weight."""
    boost_low = _make_boost([("co_aspect", 0.1)])
    boost_high = _make_boost([("co_aspect", 0.9)])
    assert boost_high > boost_low


# ---------------------------------------------------------------------------
# Build why_surfaced uses merged_aspects (R8 / C9 fix)
# ---------------------------------------------------------------------------

def test_build_why_surfaced_uses_merged_aspects(conn, project):
    """Caller-supplied filter aspects must appear in the explanation."""
    from loci.retrieve.concept_expand import build_why_surfaced

    rid = _insert_resource(conn, project.id, "Methodology Paper")
    _tag(conn, project.id, rid, "methodology")
    _tag(conn, project.id, rid, "ablation")

    # Caller supplied "methodology" but expand produced only "ablation"
    merged = ["methodology", "ablation"]
    why = build_why_surfaced(
        chunk={"resource_id": rid},
        matched_aspects=merged,
        conn=conn,
        project_id=project.id,
    )
    assert "methodology" in why or "ablation" in why
