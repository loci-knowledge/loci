"""Relevance job — focused single-pass synthesis for a project↔workspace pair.

Unlike the full reflect cycle (SYNTHESISE → SELF-CRITIQUE → APPLY), this job
runs only the synthesis stage and applies all proposed actions immediately.
It is cheaper and faster, making it suitable for high-frequency triggers:
  - link: a workspace was just linked to a project
  - incremental: new raws arrived in a linked workspace
  - profile_refresh: the project profile changed

The job payload carries:
    {
      "workspace_id": "<ULID>",   # required
      "scope": "link" | "incremental" | "profile_refresh"
    }

Without an LLM configured, the job returns skipped=True.

Enqueue-side deduplication: callers should pass a fingerprint to prevent
relevance-job storms when a workspace receives a burst of new files.
"""

from __future__ import annotations

import logging
import sqlite3

from loci.agent.interpreter import (
    _SYNTH_INSTRUCTIONS,
    Reflection,
    _apply_actions,
)
from loci.config import get_settings
from loci.graph.models import new_id
from loci.graph.projects import ProjectRepository
from loci.graph.workspaces import WorkspaceRepository
from loci.llm import LLMNotConfiguredError, build_agent

log = logging.getLogger(__name__)

# How many raws from the linked workspace to include in the relevance prompt.
WORKSPACE_RAW_SAMPLE = 20
WORKSPACE_RAW_EXCERPT = 600


def run(conn: sqlite3.Connection, project_id: str | None, payload: dict) -> dict:
    """Relevance-synthesis job handler. Signature matches worker dispatch."""
    if project_id is None:
        raise ValueError("relevance job requires a project_id")

    workspace_id = payload.get("workspace_id")
    if workspace_id is None:
        raise ValueError("relevance job requires workspace_id in payload")
    scope = payload.get("scope", "incremental")

    ws_repo = WorkspaceRepository(conn)
    workspace = ws_repo.get(workspace_id)
    if workspace is None:
        raise ValueError(f"workspace not found: {workspace_id}")

    pr = ProjectRepository(conn)
    project = pr.get(project_id)
    if project is None:
        raise ValueError(f"project not found: {project_id}")

    settings = get_settings()
    try:
        agent = build_agent(
            settings.interpretation_model,
            instructions=_SYNTH_INSTRUCTIONS,
            output_type=Reflection,
        )
    except LLMNotConfiguredError as exc:
        log.info("relevance: %s; skipping", exc)
        return {"skipped": True, "reason": str(exc), "actions_taken": 0}

    # Build a focused prompt for this workspace↔project pair.
    sample = _sample_workspace_raws(conn, workspace_id, WORKSPACE_RAW_SAMPLE, WORKSPACE_RAW_EXCERPT)
    user_msg = (
        f"PROJECT PROFILE:\n{project.profile_md or '(empty)'}\n\n"
        f"---\n\nWORKSPACE: {workspace.name} (kind={workspace.kind})\n"
        f"{workspace.description_md or ''}\n\n"
        f"---\n\nSOURCE MATERIAL FROM THIS WORKSPACE (scope={scope}):\n"
        f"{sample or '(no raws yet)'}\n\n"
        f"---\n\n"
        f"Your job: identify how this workspace's material relates to the "
        f"project's intent. Prefer creating `relevance` interpretations with "
        f"a typed angle. Name the bridge — do not summarise the sources."
    )

    try:
        result = agent.run_sync(user_msg)
    except Exception as exc:  # noqa: BLE001
        log.exception("relevance: LLM call failed")
        return {"skipped": False, "error": str(exc), "actions_taken": 0}

    reflection = result.output

    # Log to agent_reflections. Map scope to a valid trigger value.
    _VALID_TRIGGERS = frozenset(
        ["draft", "feedback", "manual", "kickoff", "link",
         "profile_refresh", "incremental", "retrieve"]
    )
    trigger = scope if scope in _VALID_TRIGGERS else "incremental"
    rid = new_id()
    conn.execute(
        """
        INSERT INTO agent_reflections(id, project_id, response_id, trigger,
                                       instruction, deliberation_md, actions_json)
        VALUES (?, ?, NULL, ?, ?, ?, '[]')
        """,
        (rid, project_id, trigger, f"relevance pass: {workspace.name}", reflection.deliberation),
    )

    # Apply all proposed actions (no critique stage — stay cheap).
    from loci.agent.interpreter import _Context
    ctx = _Context(
        instruction=f"relevance pass for workspace {workspace.name}",
        user_prompt=user_msg,
        candidate_handle_to_id={},
        cited_node_ids=set(),
        pinned_node_ids=pr.members(project_id, roles=["pinned"]),
    )
    actions_taken = _apply_actions(conn, project_id, None, ctx, reflection.actions)

    ws_repo.update_relevance_pass_ts(workspace_id, project_id)

    return {
        "skipped": False,
        "reflection_id": rid,
        "scope": scope,
        "actions_taken": actions_taken,
        "model": settings.interpretation_model,
    }


def _sample_workspace_raws(
    conn: sqlite3.Connection, workspace_id: str, n: int, excerpt_chars: int,
) -> str:
    rows = conn.execute(
        """
        SELECT n.title, n.body, n.subkind
        FROM nodes n
        JOIN workspace_membership wm ON wm.node_id = n.id
        JOIN raw_nodes r ON r.node_id = n.id
        WHERE wm.workspace_id = ?
          AND r.source_of_truth = 1
        ORDER BY n.created_at DESC
        LIMIT ?
        """,
        (workspace_id, n),
    ).fetchall()
    parts: list[str] = []
    for r in rows:
        body = (r["body"] or "").strip()[:excerpt_chars]
        if body:
            parts.append(f"- [{r['subkind']}] {r['title']}\n  {body}")
    return "\n\n".join(parts)
