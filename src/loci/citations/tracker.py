"""Persist responses + traces.

A Response is the full record of a retrieve/draft call: the original request,
the assembled output, the cited node ids, and the response id (which clients
echo back when asking "expand citation X from response Y").

A Trace is one (response_id, node_id, kind) row per node touched by a
response. We write both `retrieved` and `cited` traces so the absorb job can
later distinguish "this node was returned but not used" from "this node was
actually cited in the output."

Retention: traces grow unboundedly as the user uses the system. The PLAN's
absorb job replays them into `nodes.access_count` / `last_accessed_at` /
`confidence` and then truncates the oldest tail (keeping the last 30 days
by default — see `Settings.forgetting_inactivity_days`). That truncation is
the absorb job's responsibility, not this module's.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Literal

from loci.graph.models import new_id, now_iso

TraceKind = Literal[
    "retrieved", "cited", "edited", "accepted", "rejected", "pinned",
    "cited_kept", "cited_dropped", "cited_replaced", "requery",
    "agent_synthesised", "agent_reinforced", "agent_softened",
    "agent_updated_angle",
    "routed_via", "route_target",
]


@dataclass
class ResponseRecord:
    project_id: str
    session_id: str
    request: dict
    output: str
    cited_node_ids: list[str]
    # Per-raw provenance: list of {raw_id, raw_title, interp_path: [{id, edge, to}]}.
    # Empty for retrieve calls; populated by draft/q.
    trace_table: list[dict] = field(default_factory=list)
    client: str = "unknown"
    id: str = field(default_factory=new_id)
    ts: str = field(default_factory=now_iso)


class CitationTracker:
    """Single-purpose helper that owns the response + trace write path.

    Constructed per-request (it holds no state beyond the connection). Tests
    can monkeypatch the methods to capture writes without hitting SQLite.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def write_response(
        self,
        record: ResponseRecord,
        retrieved_node_ids: list[str] | None = None,
    ) -> str:
        """Write the response + (cited|retrieved) traces in one transaction.

        Returns the response_id so the caller can echo it back.

        `cited_node_ids` are the nodes the *output* drew on (the citation
        block). `retrieved_node_ids` (optional) are the broader set that
        retrieval surfaced — those get `kind='retrieved'` traces. Cited rows
        get `kind='cited'`. A node can appear in both lists; we emit one row
        per (kind, node) pair.

        After the SQL commit, every (node_id, action) trace is published to
        the project's WS channel so subscribers can update their TraceLayer
        in real time. Publish failures are swallowed — losing a real-time
        update is preferable to corrupting the SQL write path.
        """
        from loci.db.connection import transaction

        retrieved_node_ids = retrieved_node_ids or []
        cited_set = set(record.cited_node_ids)
        # Nodes that were retrieved but not cited.
        retrieved_only = [n for n in retrieved_node_ids if n not in cited_set]

        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO responses(id, project_id, session_id, request,
                                       output, cited_node_ids, trace_table,
                                       ts, client)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    record.id, record.project_id, record.session_id,
                    json.dumps(record.request),
                    record.output,
                    json.dumps(record.cited_node_ids),
                    json.dumps(record.trace_table),
                    record.ts, record.client,
                ),
            )
            # Per-node traces. We use executemany for the two batches.
            if record.cited_node_ids:
                self.conn.executemany(
                    """
                    INSERT INTO traces(id, project_id, session_id, response_id,
                                        node_id, kind, ts, client)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    [
                        (new_id(), record.project_id, record.session_id,
                         record.id, nid, "cited", record.ts, record.client)
                        for nid in record.cited_node_ids
                    ],
                )
            if retrieved_only:
                self.conn.executemany(
                    """
                    INSERT INTO traces(id, project_id, session_id, response_id,
                                        node_id, kind, ts, client)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    [
                        (new_id(), record.project_id, record.session_id,
                         record.id, nid, "retrieved", record.ts, record.client)
                        for nid in retrieved_only
                    ],
                )
        # After commit, fan out trace events to subscribers.
        self._publish_traces(
            record.project_id, record.session_id, record.id,
            ts=record.ts,
            cited=record.cited_node_ids, retrieved=retrieved_only,
        )
        return record.id

    def append_trace(
        self, project_id: str, node_id: str, kind: TraceKind,
        *, session_id: str = "", response_id: str | None = None,
        client: str = "unknown",
    ) -> str:
        """Single-trace write — used for explicit gestures (accept/dismiss/pin)."""
        trace_id = new_id()
        ts = now_iso()
        self.conn.execute(
            """
            INSERT INTO traces(id, project_id, session_id, response_id,
                                node_id, kind, ts, client)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (trace_id, project_id, session_id, response_id, node_id, kind,
             ts, client),
        )
        self._publish_one_trace(
            project_id=project_id, node_id=node_id, action=kind,
            ts=ts, session_id=session_id or None, response_id=response_id,
        )
        return trace_id

    # -----------------------------------------------------------------------
    # Pub/sub fan-out (best-effort)
    # -----------------------------------------------------------------------

    @staticmethod
    def _publish_one_trace(
        *, project_id: str, node_id: str, action: str, ts: str,
        session_id: str | None, response_id: str | None,
    ) -> None:
        # Imported lazily so the citations module doesn't pull pubsub at import
        # time (tests construct CitationTracker without an event loop).
        import contextlib

        try:
            from loci.api.publishers import publish_trace
        except Exception:  # noqa: BLE001
            return
        # Never let a WS publish break the SQL write path.
        with contextlib.suppress(Exception):
            publish_trace(
                project_id, node_id=node_id, action=action, ts=ts,
                session_id=session_id, response_id=response_id,
            )

    def _publish_traces(
        self, project_id: str, session_id: str, response_id: str,
        *, ts: str, cited: list[str], retrieved: list[str],
    ) -> None:
        for nid in cited:
            self._publish_one_trace(
                project_id=project_id, node_id=nid, action="cited",
                ts=ts, session_id=session_id, response_id=response_id,
            )
        for nid in retrieved:
            self._publish_one_trace(
                project_id=project_id, node_id=nid, action="retrieved",
                ts=ts, session_id=session_id, response_id=response_id,
            )

    # -----------------------------------------------------------------------
    # Reads — power the citation expansion endpoints
    # -----------------------------------------------------------------------

    def get_response(self, response_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM responses WHERE id = ?", (response_id,)
        ).fetchone()
        if row is None:
            return None
        keys = row.keys()
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "session_id": row["session_id"],
            "request": json.loads(row["request"]),
            "output": row["output"],
            "cited_node_ids": json.loads(row["cited_node_ids"]),
            "trace_table": (
                json.loads(row["trace_table"]) if "trace_table" in keys
                and row["trace_table"] else []
            ),
            "ts": row["ts"],
            "client": row["client"],
        }

    def trace_for_node(self, node_id: str, *, limit: int = 200) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT id, project_id, session_id, response_id, kind, ts, client
            FROM traces
            WHERE node_id = ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (node_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def responses_citing_node(self, node_id: str, *, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT r.id, r.project_id, r.session_id, r.ts, r.client
            FROM responses r
            JOIN traces t ON t.response_id = r.id
            WHERE t.node_id = ? AND t.kind = 'cited'
            GROUP BY r.id
            ORDER BY r.ts DESC
            LIMIT ?
            """,
            (node_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def write_response_with_traces(
    conn: sqlite3.Connection,
    record: ResponseRecord,
    retrieved_node_ids: list[str] | None = None,
) -> str:
    """One-call convenience for the most common write path."""
    return CitationTracker(conn).write_response(record, retrieved_node_ids)
