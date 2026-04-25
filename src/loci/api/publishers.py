"""Helpers that publish graph deltas to the WS bus.

Most route handlers are sync `def` (they execute on FastAPI's threadpool).
They can't `await bus.publish(...)`, so they call into these helpers, which
internally use `bus.publish_sync(...)` — that schedules the publish on the
asyncio loop attached at app startup.

Why a dedicated module? Two reasons:

1. The route layer should not have to know about pubsub channel naming or
   how to walk `project_membership` to find every project a node belongs to.
2. We sometimes need to fan an event out to multiple projects (a node can
   be in many). Centralising that walk avoids duplicating the SQL.

Event shape — matches the frontend's `deltaReducer` fallthrough:

    {"op": "upsert"|"delete", "entity": "node"|"edge",
     "payload": <node-or-edge dict>, "id": <only for delete>,
     "seq": <int>, "ts": <iso>}

The frontend's deltaReducer doesn't itself read `seq` or `ts` from the wire —
it tracks `seq` via the GraphSocket — but we include both so any future
reconnect/backfill logic has them.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from loci.api.pubsub import bus
from loci.graph.models import Edge, Node, RawNode, now_iso

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Node payload serialisation
# ---------------------------------------------------------------------------


def node_to_wire(node: Node, *, role: str | None = None) -> dict[str, Any]:
    """Render a Node into the dict the frontend's `parseNode` expects.

    Mirrors the keys used by `GET /projects/:id/graph` so the delta and the
    snapshot agree on shape.
    """
    base: dict[str, Any] = {
        "id": node.id,
        "kind": node.kind,
        "subkind": node.subkind,
        "title": node.title,
        "status": node.status,
        "confidence": node.confidence,
        "access_count": node.access_count,
        "last_accessed_at": node.last_accessed_at,
        "tags": list(node.tags),
    }
    if isinstance(node, RawNode):
        base.update(
            {
                "mime": node.mime,
                "source_of_truth": node.source_of_truth,
                "canonical_path": node.canonical_path,
            },
        )
    else:
        # Body is included for interpretation nodes — the frontend's
        # parseNode optionally stores `body` on the DTO. Raw bodies tend to
        # be heavy (full PDF text), so we skip them on the wire.
        base["body"] = node.body
    if role is not None:
        base["role"] = role
    return base


def edge_to_wire(edge: Edge) -> dict[str, Any]:
    return {
        "id": edge.id,
        "src": edge.src,
        "dst": edge.dst,
        "type": edge.type,
        "weight": edge.weight,
    }


# ---------------------------------------------------------------------------
# Project lookup (a node may live in many projects)
# ---------------------------------------------------------------------------


def projects_for_node(conn: sqlite3.Connection, node_id: str) -> list[str]:
    """Return every project_id that includes `node_id` (any role except excluded)."""
    rows = conn.execute(
        """
        SELECT project_id FROM project_membership
        WHERE node_id = ? AND role != 'excluded'
        """,
        (node_id,),
    ).fetchall()
    return [r["project_id"] for r in rows]


def projects_for_edge(conn: sqlite3.Connection, src: str, dst: str) -> list[str]:
    """Return projects that contain BOTH endpoints. The graph view filters by
    membership too, so an edge only affects projects where both endpoints are
    visible.
    """
    rows = conn.execute(
        """
        SELECT pm1.project_id
        FROM project_membership pm1
        JOIN project_membership pm2
          ON pm2.project_id = pm1.project_id AND pm2.node_id = ?
        WHERE pm1.node_id = ? AND pm1.role != 'excluded' AND pm2.role != 'excluded'
        """,
        (dst, src),
    ).fetchall()
    return [r["project_id"] for r in rows]


# ---------------------------------------------------------------------------
# Publish entry points
# ---------------------------------------------------------------------------


def publish_node_upsert(
    conn: sqlite3.Connection, node: Node, *, project_ids: list[str] | None = None,
) -> None:
    """Publish a node upsert to every project that contains it.

    `project_ids` lets callers narrow the fan-out (e.g. on `pin`, we know the
    project from the request). When omitted we walk `project_membership`.
    """
    targets = project_ids if project_ids is not None else projects_for_node(conn, node.id)
    if not targets:
        return
    payload = node_to_wire(node)
    ts = now_iso()
    for pid in targets:
        seq = bus.next_seq(pid)
        event = {
            "op": "upsert",
            "entity": "node",
            "payload": payload,
            "seq": seq,
            "ts": ts,
        }
        try:
            bus.publish_sync(f"project:{pid}", event)
        except Exception:  # noqa: BLE001
            log.exception("publishers: failed to publish node upsert for %s", pid)


def publish_node_delete(
    conn: sqlite3.Connection, node_id: str, *, project_ids: list[str] | None = None,
) -> None:
    """Publish a node deletion. Caller passes project_ids when the node is
    already gone from `project_membership`."""
    targets = project_ids if project_ids is not None else projects_for_node(conn, node_id)
    if not targets:
        return
    ts = now_iso()
    for pid in targets:
        seq = bus.next_seq(pid)
        event = {
            "op": "delete",
            "entity": "node",
            "id": node_id,
            "seq": seq,
            "ts": ts,
        }
        try:
            bus.publish_sync(f"project:{pid}", event)
        except Exception:  # noqa: BLE001
            log.exception("publishers: failed to publish node delete for %s", pid)


def publish_edge_upsert(
    conn: sqlite3.Connection, edge: Edge, *, project_ids: list[str] | None = None,
) -> None:
    targets = (
        project_ids if project_ids is not None
        else projects_for_edge(conn, edge.src, edge.dst)
    )
    if not targets:
        return
    payload = edge_to_wire(edge)
    ts = now_iso()
    for pid in targets:
        seq = bus.next_seq(pid)
        event = {
            "op": "upsert",
            "entity": "edge",
            "payload": payload,
            "seq": seq,
            "ts": ts,
        }
        try:
            bus.publish_sync(f"project:{pid}", event)
        except Exception:  # noqa: BLE001
            log.exception("publishers: failed to publish edge upsert for %s", pid)


def publish_edge_delete(
    conn: sqlite3.Connection,
    edge_id: str,
    *,
    src: str | None = None,
    dst: str | None = None,
    project_ids: list[str] | None = None,
) -> None:
    """Publish an edge deletion. Caller should pass `src`/`dst` so we can
    recover the project fan-out — the row is gone by the time this fires."""
    if project_ids is None and src is not None and dst is not None:
        project_ids = projects_for_edge(conn, src, dst)
    targets = project_ids or []
    if not targets:
        return
    ts = now_iso()
    for pid in targets:
        seq = bus.next_seq(pid)
        event = {
            "op": "delete",
            "entity": "edge",
            "id": edge_id,
            "seq": seq,
            "ts": ts,
        }
        try:
            bus.publish_sync(f"project:{pid}", event)
        except Exception:  # noqa: BLE001
            log.exception("publishers: failed to publish edge delete for %s", pid)


def publish_trace(
    project_id: str,
    *,
    node_id: str,
    action: str,
    ts: str,
    session_id: str | None = None,
    response_id: str | None = None,
) -> None:
    """Publish a trace event. `action` is one of the TraceKind values."""
    seq = bus.next_seq(project_id)
    event = {
        "kind": "trace",
        "node_id": node_id,
        "action": action,
        "ts": ts,
        "session_id": session_id,
        "response_id": response_id,
        "seq": seq,
    }
    try:
        bus.publish_sync(f"project:{project_id}", event)
    except Exception:  # noqa: BLE001
        log.exception("publishers: failed to publish trace for %s", project_id)
