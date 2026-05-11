"""Centralized MCP usage event recording.

All MCP tools call record_event() to log usage and optionally trigger
background interpretation inference.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def record_event(
    *,
    conn,
    tool: str,
    project_id: str | None,
    resource_id: str | None = None,
    query: str | None = None,
    session_hash: str | None = None,
    context_note: str | None = None,
    enqueue_inference: bool = True,
    immediate: bool = False,  # True for loci_save (bypass daily bucket)
) -> None:
    """Record a usage event for a resource and optionally enqueue inference.

    Best-effort — never raises. If resource_id is None, only inference
    enqueueing is skipped (no usage log row is written).
    """
    try:
        from loci.graph.models import new_id, now_iso

        if resource_id is not None:
            usage_id = new_id()
            conn.execute(
                "INSERT INTO resource_usage_log(id, resource_id, project_id, session_hash, "
                "tool_call_type, query, context_note, used_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (usage_id, resource_id, project_id, session_hash, tool, query, context_note, now_iso()),
            )

        if enqueue_inference and project_id and resource_id:
            from datetime import UTC, datetime

            from loci.jobs.queue import enqueue

            today = datetime.now(UTC).strftime("%Y-%m-%d")

            if immediate:
                fingerprint = None  # no dedup for initial save
            else:
                fingerprint = f"infer_interpretation:{project_id}:{resource_id}:{today}"[:64]

            enqueue(
                conn,
                kind="infer_interpretation",
                project_id=project_id,
                payload={
                    "resource_id": resource_id,
                    "project_id": project_id,
                    "trigger": tool,
                },
                fingerprint=fingerprint,
            )

            # Enqueue daily co_recalled edge refresh (R4) — once per project per day.
            enqueue(
                conn,
                kind="refresh_co_recalled_edges",
                project_id=project_id,
                payload={"project_id": project_id, "lookback_days": 30},
                fingerprint=f"co_recalled:{project_id}:{today}",
            )

    except Exception:  # noqa: BLE001
        log.debug("record_event: failed to record usage for tool=%s resource=%s", tool, resource_id, exc_info=True)


async def record_browse_events(
    *,
    conn,
    project_id: str | None,
    resource_ids: list[str],
    session_hash: str | None,
    tool: str = "loci_browse",
) -> None:
    """Record browse usage events for a list of resource IDs.

    Calls record_event for each resource_id with enqueue_inference=False.
    Best-effort — never raises.
    """
    for rid in resource_ids:
        await record_event(
            conn=conn,
            tool=tool,
            project_id=project_id,
            resource_id=rid,
            session_hash=session_hash,
            enqueue_inference=False,
        )
