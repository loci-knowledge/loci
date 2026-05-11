"""Tests for the infer_interpretation background job."""

from __future__ import annotations

import asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_resource(conn, node_id: str, title: str = "Test Paper", body: str = "body text") -> None:
    """Insert a minimal nodes + raw_nodes row."""
    content_hash = node_id[:16]
    conn.execute(
        "INSERT INTO nodes(id, kind, subkind, title, body) VALUES (?,?,?,?,?)",
        (node_id, "raw", "md", title, body),
    )
    conn.execute(
        "INSERT INTO raw_nodes(id, content_hash, canonical_path, mime, size_bytes) "
        "VALUES (?,?,?,?,?)",
        (node_id, content_hash, f"/tmp/{node_id}.md", "text/markdown", len(body)),
    )


def _link_resource_to_project(conn, project_id: str, resource_id: str) -> None:
    """Add resource to a project via project_membership (included)."""
    from loci.graph.models import new_id, now_iso
    conn.execute(
        "INSERT OR IGNORE INTO project_membership(project_id, node_id, role, added_at, added_by) "
        "VALUES (?,?,?,?,?)",
        (project_id, resource_id, "included", now_iso(), "test"),
    )


def _run(coro):
    """Run a coroutine synchronously in a fresh event loop."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_freshness_gate(conn, project, monkeypatch):
    """When inputs_hash is unchanged, the LLM is not called."""
    rid = "01BBBBBBBBBBBBBBBBBBBBAA00"
    _insert_resource(conn, rid, title="Existing Doc", body="hello world")
    _link_resource_to_project(conn, project.id, rid)

    # Pre-compute and store the same inputs_hash that the job will compute.
    from loci.graph.aspects import AspectRepository
    from loci.graph.interpretations import ProjectInterpretationRepository
    from loci.graph.models import ProjectInterpretation, now_iso

    aspect_repo = AspectRepository(conn)
    interp_repo = ProjectInterpretationRepository(conn)
    top_pairs = aspect_repo.top_aspects(project.id, limit=10)
    aspect_labels = [lbl for lbl, _ in top_pairs]

    # content_hash stored in raw_nodes is node_id[:16] (see _insert_resource helper)
    inputs_hash = interp_repo.compute_inputs_hash(rid[:16], "", aspect_labels, [])

    # Write the interpretation row with the matching hash.
    interp_repo.upsert(
        ProjectInterpretation(
            project_id=project.id,
            resource_id=rid,
            summary_md="existing summary",
            stance="reference",
            inputs_hash=inputs_hash,
            model_id=None,
            generated_at=now_iso(),
        )
    )

    # Patch build_agent so any call to it raises — should never be reached.
    import loci.capture.aspect_suggest as asp_mod

    def boom(*args, **kwargs):
        raise AssertionError("LLM should not be called when inputs are unchanged")

    monkeypatch.setattr(asp_mod, "build_agent", boom, raising=False)

    from loci.jobs.infer_interpretation import handle_infer_interpretation
    from loci.config import get_settings

    job = {
        "id": "jid-freshness",
        "project_id": project.id,
        "payload": {"resource_id": rid, "project_id": project.id, "trigger": "test"},
    }
    result = _run(handle_infer_interpretation(job, conn, get_settings()))
    assert result.get("skipped") == "inputs_unchanged", f"unexpected result: {result}"


def test_writes_pra_rows(conn, project, monkeypatch):
    """On first run (no existing interpretation), project_resource_aspects rows are written."""
    rid = "01BBBBBBBBBBBBBBBBBBBBAA01"
    _insert_resource(conn, rid, title="New Paper", body="a new document about methodology")
    _link_resource_to_project(conn, project.id, rid)

    # Mock classify_project_interpretation_llm to return a canned output.
    import loci.jobs.infer_interpretation as job_mod

    from loci.graph.models import AspectScore, InterpretationOutput

    fake_output = InterpretationOutput(
        aspects=[AspectScore(label="methodology", confidence=0.85)],
        summary_md="This document is about methodology.",
        stance="methodological",
        relations=[],
    )

    async def fake_llm(**kwargs):
        return fake_output

    monkeypatch.setattr(
        "loci.capture.aspect_suggest.classify_project_interpretation_llm",
        fake_llm,
    )

    from loci.jobs.infer_interpretation import handle_infer_interpretation
    from loci.config import get_settings

    job = {
        "id": "jid-writes-pra",
        "project_id": project.id,
        "payload": {"resource_id": rid, "project_id": project.id, "trigger": "test"},
    }
    result = _run(handle_infer_interpretation(job, conn, get_settings()))

    assert result.get("aspects_written", 0) >= 1, f"expected aspects_written ≥ 1, got: {result}"

    rows = conn.execute(
        "SELECT * FROM project_resource_aspects WHERE project_id = ? AND resource_id = ?",
        (project.id, rid),
    ).fetchall()
    assert rows, "expected at least one project_resource_aspects row"
    sources = {r["source"] for r in rows}
    assert "llm" in sources, f"expected source='llm' among {sources}"


def test_user_gold_protected(conn, project, monkeypatch):
    """source='user' rows must not be overwritten even when the LLM outputs the same aspect."""
    rid = "01BBBBBBBBBBBBBBBBBBBBAA02"
    _insert_resource(conn, rid, title="Gold Doc", body="user curated content on methodology")
    _link_resource_to_project(conn, project.id, rid)

    # Pre-insert a user-gold aspect row.
    from loci.graph.aspects import AspectRepository

    repo = AspectRepository(conn)
    aspect = repo.ensure_aspect("methodology", source="user")
    from loci.graph.models import now_iso
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO project_resource_aspects(
            project_id, resource_id, aspect_id, confidence, source,
            weight_signals_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'user', NULL, ?, ?)
        """,
        (project.id, rid, aspect.id, 1.0, ts, ts),
    )

    # Mock LLM to output the same aspect with lower confidence.
    from loci.graph.models import AspectScore, InterpretationOutput

    fake_output = InterpretationOutput(
        aspects=[AspectScore(label="methodology", confidence=0.3)],
        summary_md="Methodology paper.",
        stance="methodological",
        relations=[],
    )

    async def fake_llm(**kwargs):
        return fake_output

    monkeypatch.setattr(
        "loci.capture.aspect_suggest.classify_project_interpretation_llm",
        fake_llm,
    )

    from loci.jobs.infer_interpretation import handle_infer_interpretation
    from loci.config import get_settings

    job = {
        "id": "jid-gold-protected",
        "project_id": project.id,
        "payload": {"resource_id": rid, "project_id": project.id, "trigger": "test"},
    }
    _run(handle_infer_interpretation(job, conn, get_settings()))

    row = conn.execute(
        """
        SELECT pra.source, pra.confidence
        FROM project_resource_aspects pra
        JOIN aspect_vocab av ON av.id = pra.aspect_id
        WHERE pra.project_id = ? AND pra.resource_id = ? AND av.label = 'methodology'
        """,
        (project.id, rid),
    ).fetchone()

    assert row is not None
    assert row["source"] == "user", f"user gold row was overwritten: source={row['source']!r}"
    assert abs(row["confidence"] - 1.0) < 1e-6, "user confidence should be unchanged"
