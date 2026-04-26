"""Draft pipeline tests.

We test the candidate-rendering and locus-grouping logic without an LLM
(those functions are pure given a list of `RetrievedNode`s + routing data).
The full `draft()` end-to-end runs in unconfigured mode (no API key) and
returns a candidate-only stub, which is enough to verify the orchestration
plumbing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from loci.draft import DraftRequest, _format_candidates
from loci.draft import draft as run_draft
from loci.graph import (
    EdgeRepository,
    InterpretationNode,
    NodeRepository,
    ProjectRepository,
)
from loci.ingest import scan_path
from loci.retrieve import RetrievedNode, RoutingInterp


@pytest.fixture
def long_corpus(tmp_path: Path) -> Path:
    src = tmp_path / "corpus"
    src.mkdir()
    (src / "rotary.md").write_text(
        "# Rotary Embeddings\n\n"
        + ("RoFormer rotates positions into the projections. " * 60),
    )
    (src / "cross.md").write_text(
        "# Cross Attention\n\n"
        + ("Cross attention reads keys and values from a different sequence. " * 60),
    )
    return src


def test_format_candidates_groups_by_routing_locus():
    """Two raws routed by the same locus end up in one group; a direct hit
    sits in its own 'direct hits' group."""
    # Synthetic candidates — we don't go through retrieve here to keep the
    # test focused on the rendering logic.
    cand_routed_a = RetrievedNode(
        node_id="01" + "A" * 24, kind="raw", subkind="md",
        title="Raw A", snippet="Aaaaa", score=0.9, why="routed",
        chunk_id="chunkA", chunk_text="Aaaaa span text", chunk_section="## Section",
    )
    cand_routed_b = RetrievedNode(
        node_id="01" + "B" * 24, kind="raw", subkind="md",
        title="Raw B", snippet="Bbbbb", score=0.7, why="routed",
        chunk_id="chunkB", chunk_text="Bbbbb span text",
    )
    cand_direct = RetrievedNode(
        node_id="01" + "C" * 24, kind="raw", subkind="md",
        title="Raw C", snippet="Ccccc", score=0.5, why="direct",
        chunk_id="chunkC", chunk_text="Ccccc span text",
    )
    locus_id = "01" + "L" * 24
    # Two raws share locus L; raw C has no trace.
    trace_by_raw = {
        cand_routed_a.node_id: {
            "raw_id": cand_routed_a.node_id, "raw_title": "Raw A",
            "interp_path": [{"id": locus_id, "edge": "cites", "to": cand_routed_a.node_id}],
        },
        cand_routed_b.node_id: {
            "raw_id": cand_routed_b.node_id, "raw_title": "Raw B",
            "interp_path": [{"id": locus_id, "edge": "cites", "to": cand_routed_b.node_id}],
        },
        cand_direct.node_id: {
            "raw_id": cand_direct.node_id, "raw_title": "Raw C",
            "interp_path": [],
        },
    }
    routing_by_id = {
        locus_id: RoutingInterp(
            node_id=locus_id, subkind="decision", title="Routing locus L",
            relation_md="Locus L explains why raws A and B matter.",
            overlap_md="Both rotate the residual stream.",
            source_anchor_md="Section 3.2.",
            angle=None, score=0.6,
        ),
    }

    # We don't need a real DB connection because both raws are unknown to
    # NodeRepository.get_many() — the renderer just skips them. To still
    # exercise the rendering, we patch get_many to return a fake node row.
    class _FakeNode:
        def __init__(self, nid, title):
            self.id = nid
            self.kind = "raw"
            self.subkind = "md"
            self.title = title
            self.body = "body of " + title

    class _FakeNodes:
        def get_many(self, ids):
            mapping = {
                cand_routed_a.node_id: _FakeNode(cand_routed_a.node_id, "Raw A"),
                cand_routed_b.node_id: _FakeNode(cand_routed_b.node_id, "Raw B"),
                cand_direct.node_id: _FakeNode(cand_direct.node_id, "Raw C"),
            }
            return [mapping[i] for i in ids if i in mapping]

    # Monkey-patch the NodeRepository constructor inside _format_candidates.
    import loci.draft as draft_mod

    class _StubConn:
        pass

    orig_repo = draft_mod.NodeRepository
    draft_mod.NodeRepository = lambda conn: _FakeNodes()  # type: ignore[assignment]
    try:
        block, handle_to_id, handle_to_chunk = _format_candidates(
            [cand_routed_a, cand_routed_b, cand_direct], _StubConn(),
            trace_by_raw=trace_by_raw, routing_by_id=routing_by_id,
        )
    finally:
        draft_mod.NodeRepository = orig_repo  # type: ignore[assignment]

    # Three handles assigned in order; routed group first (sorted by locus
    # score), direct hits last.
    assert set(handle_to_id.keys()) == {"C1", "C2", "C3"}
    # Group header for L appears before the direct hits group.
    locus_pos = block.find("Routing locus L")
    direct_pos = block.find("direct hits")
    assert locus_pos != -1 and direct_pos != -1
    assert locus_pos < direct_pos
    # Within the routed group, A (score 0.9) precedes B (score 0.7).
    a_pos = block.find("Raw A")
    b_pos = block.find("Raw B")
    assert a_pos != -1 and b_pos != -1 and a_pos < b_pos
    # Chunk text (not first 800 chars of body) appears in the rendered block.
    assert "Aaaaa span text" in block
    assert "Bbbbb span text" in block
    # ROUTING locus header surfaces relation/overlap/anchor.
    assert "explains why raws A and B matter" in block
    assert "rotate the residual stream" in block
    # handle_to_chunk_text fed to verifier is populated.
    assert all(handle_to_chunk[h] for h in ("C1", "C2", "C3"))


def _force_unconfigured_llm(monkeypatch):
    """Stub `build_agent` so it raises LLMNotConfiguredError. The user's
    .env may have a real key — patching the function is more reliable than
    deleting env vars (pydantic-settings reads .env at construction)."""
    from loci import draft as draft_mod
    from loci import verify as verify_mod
    from loci.llm import LLMNotConfiguredError

    def _raise(*a, **kw):
        raise LLMNotConfiguredError("test stub: LLM disabled")

    monkeypatch.setattr(draft_mod, "build_agent", _raise)
    monkeypatch.setattr(verify_mod, "build_agent", _raise)


def test_draft_returns_candidate_stub_without_llm(
    conn, fake_embedder, project, workspace, long_corpus, monkeypatch,
):
    """In unconfigured mode (no LLM), draft() returns a stub that lists the
    candidates. Citation/verdict pipelines should be empty but well-formed."""
    _force_unconfigured_llm(monkeypatch)
    scan_path(conn, workspace.id, long_corpus, embedder=fake_embedder)

    res = run_draft(conn, DraftRequest(
        project_id=project.id, session_id="test",
        instruction="Write a paragraph about rotary embeddings.",
        verify=False,
    ))
    assert res.candidate_count >= 1
    assert "loci is in unconfigured mode" in res.output_md
    # Stub has no [Cn] markers, so no citations and no verdicts.
    assert res.citations == []
    assert res.verdicts == []
    assert isinstance(res.routing_loci, list)
    assert isinstance(res.trace_table, list)


def test_draft_citation_carries_chunk_id_when_routed(
    conn, fake_embedder, project, workspace, long_corpus, monkeypatch,
):
    """When the LLM is unavailable but candidates are chunked, the retrieval
    layer still surfaces chunk_id on each RetrievedNode. Smoke-test of the
    chunk-id flow through retrieve → draft."""
    _force_unconfigured_llm(monkeypatch)

    scan_path(conn, workspace.id, long_corpus, embedder=fake_embedder)
    # Synthesise an interp node that cites the rotary raw — this puts the raw
    # into a routed group.
    rotary_id = conn.execute(
        "SELECT id FROM nodes WHERE title='Rotary Embeddings'",
    ).fetchone()["id"]
    locus = InterpretationNode(
        subkind="decision", title="Position machinery",
        body="",
        relation_md="Rotary embeddings supply the position machinery.",
        overlap_md="Both projects rotate the residual stream.",
        source_anchor_md="Section 3.2.",
        origin="user_explicit_create",
    )
    NodeRepository(conn).create_interpretation(
        locus, embedding=fake_embedder.encode("rotary position"),
    )
    ProjectRepository(conn).add_member(project.id, locus.id, role="included")
    EdgeRepository(conn).create(locus.id, rotary_id, type="cites")

    res = run_draft(conn, DraftRequest(
        project_id=project.id, session_id="test",
        instruction="Discuss rotary embeddings.",
        verify=False,
    ))
    assert res.candidate_count >= 1
    # The retrieved node corresponding to the rotary raw should carry a chunk_id.
    rotary_cand = next((c for c in res.retrieved_node_ids if c == rotary_id), None)
    assert rotary_cand is not None
