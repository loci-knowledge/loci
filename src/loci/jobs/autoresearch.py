"""Autoresearch job — paper search + sandbox code execution → loci graph.

The user gives a query and a workspace. The handler:
  1. Picks an output directory under the workspace's first source root,
     `<root>/research/<run_id>/`.
  2. Runs `loci.research.run_research()` — a pydantic-ai sub-agent that
     searches papers (HF + Semantic Scholar + arXiv) and (if HF_TOKEN is
     present) runs code in an HF Spaces sandbox. The agent saves artifacts
     (paper notes, code, the final summary) to the output dir.
  3. Re-scans the workspace so the new artifact files become raw nodes.
  4. Creates a `relevance` interpretation node summarising the run, with
     `cites` edges to the freshly-ingested raws so retrieval can route to
     them.
  5. Enqueues a `relevance` follow-up job to deepen bridge coverage.

Payload shape:
    {
        "query": str,                 # the research question
        "workspace_id": str,          # destination workspace (sources scanned for output)
        "hf_owner": str | None,       # HF user/org for the sandbox
        "hardware": str | None,       # HF Spaces hardware tier
        "sandbox": bool,              # default True; False to skip code exec
        "max_iterations": int,        # agent loop cap; default 30
        "model": str | None,          # override Settings.research_model
    }
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from loci.config import get_settings
from loci.embed.local import get_embedder
from loci.graph import EdgeRepository, NodeRepository, ProjectRepository
from loci.graph.models import InterpretationNode, new_id
from loci.graph.workspaces import WorkspaceRepository
from loci.ingest.pipeline import scan_workspace
from loci.jobs.queue import append_job_step, enqueue, set_progress
from loci.research import run_research

log = logging.getLogger(__name__)


def run(conn: sqlite3.Connection, project_id: str | None, payload: dict) -> dict:
    """Autoresearch handler. Worker dispatch signature: (conn, project_id, payload) -> result."""
    if project_id is None:
        raise ValueError("autoresearch requires a project_id")

    query = (payload.get("query") or "").strip()
    if not query:
        raise ValueError("autoresearch payload must include 'query'")

    workspace_id = payload.get("workspace_id")
    if not workspace_id:
        raise ValueError("autoresearch payload must include 'workspace_id'")

    project = ProjectRepository(conn).get(project_id)
    if project is None:
        raise ValueError(f"project not found: {project_id}")

    ws_repo = WorkspaceRepository(conn)
    workspace = ws_repo.get(workspace_id)
    if workspace is None:
        raise ValueError(f"workspace not found: {workspace_id}")

    sources = ws_repo.list_sources(workspace_id)
    if not sources:
        raise ValueError(
            f"workspace {workspace.slug} has no source roots; "
            "register one with `loci workspace add-source` first.",
        )

    run_id = new_id()[:8]
    from loci.jobs.research_paths import resolve_research_output_dir, resolve_research_source_root
    output_dir = resolve_research_output_dir(run_id)
    research_root = resolve_research_source_root(run_id)

    settings = get_settings()
    enable_sandbox = bool(payload.get("sandbox", False))
    hf_owner = payload.get("hf_owner") or os.environ.get("HF_OWNER") or settings.research_hf_owner
    if enable_sandbox and not hf_owner:
        log.warning("autoresearch: no hf_owner set; sandbox disabled.")
        enable_sandbox = False

    hardware = payload.get("hardware") or settings.research_sandbox_hardware
    template = payload.get("template") or settings.research_template_space
    model_spec = payload.get("model") or settings.research_model
    max_iterations = int(payload.get("max_iterations") or 30)

    job_id = payload.get("__job_id")  # optional progress hook (worker doesn't pass this)

    if job_id:
        set_progress(conn, job_id, 0.05)

    def _on_step(tool_name: str, msg: str) -> None:
        if job_id:
            append_job_step(conn, job_id, tool_name, msg)

    log.info(
        "autoresearch: project=%s workspace=%s output=%s sandbox=%s hf_owner=%s",
        project.slug, workspace.slug, output_dir, enable_sandbox, hf_owner,
    )

    report = run_research(
        query=query,
        output_dir=output_dir,
        hf_owner=hf_owner if enable_sandbox else None,
        sandbox_hardware=hardware,
        sandbox_template=template,
        enable_sandbox=enable_sandbox,
        max_iterations=max_iterations,
        project_profile_md=project.profile_md or "",
        model_spec=model_spec,
        on_step=_on_step,
    )

    if report.skipped:
        return {
            "skipped": True,
            "reason": report.skip_reason,
            "output_dir": str(output_dir),
            "artifacts": report.artifacts,
        }

    if job_id:
        set_progress(conn, job_id, 0.7)

    # Ensure the research output dir is a registered source so scan_workspace picks it up.
    _ensure_research_source(conn, workspace_id, research_root)

    # The agent has written artifacts under `output_dir`. Re-scan the workspace
    # so the new files become raw nodes; existing files are deduped by content
    # hash so this is cheap.
    scan_result = scan_workspace(conn, workspace_id)
    log.info(
        "autoresearch: scan added %d new raws, deduped %d, skipped %d",
        scan_result.new_raw, scan_result.deduped, scan_result.skipped,
    )

    # Find the raw nodes whose canonical_path is under our output dir — those
    # are the artifacts this run produced. Even if they were de-duped (rare),
    # they'll still be members of this workspace.
    output_prefix = str(output_dir.resolve())
    rows = conn.execute(
        """
        SELECT n.id, n.title, n.subkind
        FROM nodes n
        JOIN raw_nodes r ON r.node_id = n.id
        JOIN workspace_membership wm ON wm.node_id = n.id
        WHERE wm.workspace_id = ?
          AND r.canonical_path LIKE ?
          AND n.status = 'live'
        ORDER BY n.created_at DESC
        """,
        (workspace_id, output_prefix + "%"),
    ).fetchall()
    artifact_node_ids: list[str] = [r["id"] for r in rows]

    if job_id:
        set_progress(conn, job_id, 0.85)

    summary_locus_id: str | None = None
    if report.summary_md and artifact_node_ids:
        summary_locus_id = _create_summary_locus(
            conn,
            project_id=project_id,
            query=query,
            report_summary=report.summary_md,
            artifact_node_ids=artifact_node_ids,
            output_dir_name=output_dir.name,
            sandbox_url=report.sandbox_url,
        )

    # Kick off a relevance pass so the new artifacts get bridged into the
    # project graph beyond the single summary locus.
    relevance_job_id = enqueue(
        conn, kind="relevance", project_id=project_id,
        payload={"workspace_id": workspace_id, "scope": "autoresearch"},
    )

    if job_id:
        set_progress(conn, job_id, 1.0)

    return {
        "output_dir": str(output_dir),
        "artifacts": report.artifacts,
        "artifact_node_ids": artifact_node_ids,
        "summary_locus_id": summary_locus_id,
        "summary_md": report.summary_md,
        "used_papers": report.used_papers,
        "sandbox_url": report.sandbox_url,
        "iterations": report.iterations,
        "relevance_job_id": relevance_job_id,
        "scan": {
            "new_raw": scan_result.new_raw,
            "deduped": scan_result.deduped,
            "skipped": scan_result.skipped,
        },
    }


def _create_summary_locus(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    query: str,
    report_summary: str,
    artifact_node_ids: list[str],
    output_dir_name: str,
    sandbox_url: str | None,
) -> str | None:
    """Create one `relevance` locus that cites every produced artifact."""
    # First non-empty line of the summary becomes the relation_md anchor; the
    # body keeps the full markdown so retrieval can score against it.
    lead = next(
        (line.strip() for line in report_summary.splitlines() if line.strip() and not line.startswith("#")),
        report_summary[:300],
    )
    relation_md = (
        f"Auto-research run answering: {query[:160]}. "
        f"{lead[:300]}"
    ).strip()
    overlap_md = (
        f"Bridges the project's question to {len(artifact_node_ids)} freshly-discovered "
        f"source(s) — all under workspace path `research/{output_dir_name}/`."
    )
    anchor_parts = [
        f"See `research/{output_dir_name}/SUMMARY.md` for the full write-up.",
    ]
    if sandbox_url:
        anchor_parts.append(f"Sandbox transcript: {sandbox_url}")
    source_anchor_md = " ".join(anchor_parts)

    title = f"research: {query[:80]}"

    locus = InterpretationNode(
        subkind="relevance",
        title=title,
        body=report_summary[:2000],  # truncated; full text lives in SUMMARY.md raw
        relation_md=relation_md[:1000],
        overlap_md=overlap_md[:600],
        source_anchor_md=source_anchor_md[:600],
        angle="applicable_pattern",
        origin="agent_synthesis",
        confidence=0.6,
        status="live",
    )

    try:
        embedder = get_embedder()
        emb_text = "\n\n".join(
            p for p in [locus.title, locus.relation_md, locus.overlap_md, locus.source_anchor_md] if p
        )
        emb = embedder.encode(emb_text) if emb_text else None
    except Exception as exc:  # noqa: BLE001
        log.warning("autoresearch: embedder load failed: %s", exc)
        emb = None

    NodeRepository(conn).create_interpretation(locus, embedding=emb)
    ProjectRepository(conn).add_member(project_id, locus.id, role="included", added_by="agent")

    edges_repo = EdgeRepository(conn)
    for raw_id in artifact_node_ids:
        try:
            edges_repo.create(locus.id, raw_id, type="cites", created_by="system")
        except Exception as exc:  # noqa: BLE001
            log.warning("autoresearch: cites edge failed (%s → %s): %s", locus.id, raw_id, exc)

    return locus.id


def _ensure_research_source(
    conn: sqlite3.Connection,
    workspace_id: str,
    research_root: Path,
) -> None:
    """Register research_root as a workspace source if not already present."""
    root_str = str(research_root.resolve())
    existing = conn.execute(
        "SELECT id FROM workspace_sources WHERE workspace_id = ? AND root_path = ?",
        (workspace_id, root_str),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO workspace_sources (id, workspace_id, root_path) "
            "VALUES (?, ?, ?)",
            (new_id(), workspace_id, root_str),
        )
        conn.commit()
        log.info("autoresearch: registered research source root %s", root_str)
