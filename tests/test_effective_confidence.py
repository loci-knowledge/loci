"""Tests for aspect_effective_confidence view and its effect on ranking.

Covers §8 verification item #5 from the petal plan:
- Base confidence when no provenance == pra.confidence
- Confirmed actions add +0.05 each (capped at 1.0)
- Rejected actions subtract -0.10 each (floored at 0.0)
- Usage events add a small positive boost
- Changes propagate into aspect_overlap_rank ordering
"""

from __future__ import annotations

import pytest

from loci.graph.aspects import AspectRepository
from loci.graph.models import AspectScore, new_id, now_iso
from loci.retrieve.concept_expand import aspect_overlap_rank


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


def _tag(conn, project_id: str, resource_id: str, label: str,
         confidence: float = 0.5, source: str = "llm") -> str:
    """Tag resource, return aspect_id."""
    from loci.graph.aspect_dsl import parse as _parse, render as _render
    prop = _parse(label)
    score = AspectScore(label=_render(prop), confidence=confidence,
                        rationale=None, proposition=prop)
    AspectRepository(conn).tag_resource_with_propositions(
        project_id, resource_id, [score], source=source
    )
    row = conn.execute(
        "SELECT av.id FROM aspect_vocab av"
        " JOIN project_resource_aspects pra ON pra.aspect_id = av.id"
        " WHERE pra.project_id = ? AND pra.resource_id = ? LIMIT 1",
        (project_id, resource_id),
    ).fetchone()
    return row["id"]


def _add_provenance(conn, project_id: str, resource_id: str,
                    aspect_id: str, action: str, source: str = "user") -> None:
    conn.execute(
        "INSERT INTO aspect_provenance(id, project_id, resource_id, aspect_id,"
        " action, source, recorded_at) VALUES (?,?,?,?,?,?,?)",
        (new_id(), project_id, resource_id, aspect_id, action, source, now_iso()),
    )


def _add_usage(conn, project_id: str, resource_id: str, n: int = 1) -> None:
    for _ in range(n):
        conn.execute(
            "INSERT INTO resource_usage_log(id, resource_id, project_id,"
            " session_hash, tool_call_type, used_at) VALUES (?,?,?,?,?,datetime('now'))",
            (new_id(), resource_id, project_id, new_id(), "loci_recall"),
        )


def _effective_confidence(conn, project_id: str, resource_id: str,
                           aspect_id: str) -> float | None:
    row = conn.execute(
        "SELECT effective_confidence FROM aspect_effective_confidence"
        " WHERE project_id = ? AND resource_id = ? AND aspect_id = ?",
        (project_id, resource_id, aspect_id),
    ).fetchone()
    return row["effective_confidence"] if row else None


# ---------------------------------------------------------------------------
# View existence
# ---------------------------------------------------------------------------

def test_view_exists(conn):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view'"
        " AND name='aspect_effective_confidence'"
    ).fetchone()
    assert row is not None, "aspect_effective_confidence view not found"


# ---------------------------------------------------------------------------
# Base confidence — no provenance
# ---------------------------------------------------------------------------

def test_base_confidence_equals_pra_confidence(conn, project):
    rid = _insert_resource(conn, project.id)
    aid = _tag(conn, project.id, rid, "reproducibility", confidence=0.7)

    ec = _effective_confidence(conn, project.id, rid, aid)
    assert ec is not None
    # With no provenance and no usage events, LOG(1+0) = 0 so ec == pra.confidence
    assert abs(ec - 0.7) < 1e-4


# ---------------------------------------------------------------------------
# Confirmed actions boost (+0.05 each)
# ---------------------------------------------------------------------------

def test_confirmed_action_increases_confidence(conn, project):
    rid = _insert_resource(conn, project.id)
    aid = _tag(conn, project.id, rid, "reproducibility", confidence=0.5)

    _add_provenance(conn, project.id, rid, aid, "confirmed", source="user")
    ec = _effective_confidence(conn, project.id, rid, aid)
    assert ec is not None
    assert ec > 0.5
    assert abs(ec - 0.55) < 1e-4


def test_multiple_confirmed_actions_accumulate(conn, project):
    rid = _insert_resource(conn, project.id)
    aid = _tag(conn, project.id, rid, "bayesian", confidence=0.5)

    for _ in range(3):
        _add_provenance(conn, project.id, rid, aid, "confirmed", source="user")

    ec = _effective_confidence(conn, project.id, rid, aid)
    assert ec is not None
    assert abs(ec - 0.65) < 1e-4  # 0.5 + 3 * 0.05


def test_confirmed_clamped_at_one(conn, project):
    rid = _insert_resource(conn, project.id)
    aid = _tag(conn, project.id, rid, "deep-learning", confidence=0.9)

    # 3 confirmed × 0.05 = +0.15 → would be 1.05 → clamped to 1.0
    for _ in range(3):
        _add_provenance(conn, project.id, rid, aid, "confirmed", source="user")

    ec = _effective_confidence(conn, project.id, rid, aid)
    assert ec == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Rejected actions decay (-0.10 each)
# ---------------------------------------------------------------------------

def test_rejected_action_decreases_confidence(conn, project):
    rid = _insert_resource(conn, project.id)
    aid = _tag(conn, project.id, rid, "statistics", confidence=0.8)

    _add_provenance(conn, project.id, rid, aid, "rejected")
    ec = _effective_confidence(conn, project.id, rid, aid)
    assert ec is not None
    assert abs(ec - 0.7) < 1e-4  # 0.8 - 0.10


def test_rejected_floored_at_zero(conn, project):
    rid = _insert_resource(conn, project.id)
    aid = _tag(conn, project.id, rid, "concept", confidence=0.2)

    # 3 rejections × 0.10 = -0.30 → would be -0.10 → floored at 0.0
    for _ in range(3):
        _add_provenance(conn, project.id, rid, aid, "rejected")

    ec = _effective_confidence(conn, project.id, rid, aid)
    assert ec == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Usage events provide a small positive boost
# ---------------------------------------------------------------------------

def test_usage_events_increase_effective_confidence(conn, project):
    rid = _insert_resource(conn, project.id)
    aid = _tag(conn, project.id, rid, "methodology", confidence=0.5)

    baseline = _effective_confidence(conn, project.id, rid, aid)
    _add_usage(conn, project.id, rid, n=5)
    boosted = _effective_confidence(conn, project.id, rid, aid)

    assert boosted > baseline


def test_usage_boost_saturates(conn, project):
    """Many usage events don't push past 1.0 (LOG saturates at 6 * 0.05 = 0.30)."""
    rid = _insert_resource(conn, project.id)
    aid = _tag(conn, project.id, rid, "technique", confidence=0.8)

    _add_usage(conn, project.id, rid, n=100)
    ec = _effective_confidence(conn, project.id, rid, aid)
    assert ec <= 1.0


# ---------------------------------------------------------------------------
# Effect on aspect_overlap_rank
# ---------------------------------------------------------------------------

def test_confirmed_resource_ranks_higher_in_overlap_rank(conn, project):
    """Resource with confirmed provenance outranks otherwise-identical peer."""
    rid_confirmed = _insert_resource(conn, project.id, "Confirmed")
    rid_plain = _insert_resource(conn, project.id, "Plain")

    aid_confirmed = _tag(conn, project.id, rid_confirmed, "reproducibility", confidence=0.5)
    _tag(conn, project.id, rid_plain, "reproducibility", confidence=0.5)

    _add_provenance(conn, project.id, rid_confirmed, aid_confirmed, "confirmed", source="user")

    results = aspect_overlap_rank(conn, project.id, ["reproducibility"], limit=10)
    ids = [r for r, _ in results]

    assert rid_confirmed in ids
    assert rid_plain in ids
    assert ids.index(rid_confirmed) < ids.index(rid_plain)


def test_rejected_resource_ranks_lower_in_overlap_rank(conn, project):
    """Resource with rejected provenance ranks below an unmodified peer."""
    rid_rejected = _insert_resource(conn, project.id, "Rejected")
    rid_plain = _insert_resource(conn, project.id, "Plain")

    aid_rejected = _tag(conn, project.id, rid_rejected, "bayesian", confidence=0.8)
    _tag(conn, project.id, rid_plain, "bayesian", confidence=0.8)

    _add_provenance(conn, project.id, rid_rejected, aid_rejected, "rejected")

    results = aspect_overlap_rank(conn, project.id, ["bayesian"], limit=10)
    ids = [r for r, _ in results]

    assert rid_rejected in ids
    assert rid_plain in ids
    assert ids.index(rid_plain) < ids.index(rid_rejected)


def test_usage_boosted_resource_ranks_higher(conn, project):
    """Frequently-recalled resource outranks unrecalled peer with same base confidence."""
    rid_recalled = _insert_resource(conn, project.id, "Recalled")
    rid_cold = _insert_resource(conn, project.id, "Cold")

    _tag(conn, project.id, rid_recalled, "statistics", confidence=0.5)
    _tag(conn, project.id, rid_cold, "statistics", confidence=0.5)

    _add_usage(conn, project.id, rid_recalled, n=10)

    results = aspect_overlap_rank(conn, project.id, ["statistics"], limit=10)
    ids = [r for r, _ in results]

    assert ids.index(rid_recalled) < ids.index(rid_cold)
