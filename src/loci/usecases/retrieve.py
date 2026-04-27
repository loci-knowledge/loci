"""Shared retrieve orchestration.

Callers (MCP, HTTP, CLI) resolve project_id themselves, then pass it here.
This module owns: Retriever → CitationTracker → reflect enqueue.
Broadcasting (IPC for MCP, in-process bus for HTTP) is left to the adapter.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RetrieveResult:
    """Canonical output from a retrieve call, before surface-specific formatting."""
    response: Any  # loci.retrieve.RetrievalResponse
    trace_id: str
    pending_effects: list[dict[str, Any]] = field(default_factory=list)


def run_retrieve(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    query: str,
    k: int = 10,
    anchors: list[str] | None = None,
    hyde: bool = False,
    include: list[str] | None = None,
    session_id: str = "default",
    client: str = "unknown",
) -> RetrieveResult:
    """Run retrieval and persist the response record.

    Returns RetrieveResult with the raw RetrievalResponse, the persisted
    trace_id, and any pending_effects (reflect job) for the caller to include
    in its response payload.
    """
    from loci.citations import CitationTracker, ResponseRecord
    from loci.retrieve import RetrievalRequest, Retriever
    from loci.retrieve.effects import (
        maybe_enqueue_retrieve_reflect,
        pending_effects_from_reflect,
    )

    resp = Retriever(conn).retrieve(RetrievalRequest(
        project_id=project_id,
        query=query,
        k=k,
        anchors=anchors or [],
        include=include,
        hyde=hyde,
    ))
    record = ResponseRecord(
        project_id=project_id,
        session_id=session_id,
        request={"query": query, "k": k, "hyde": hyde},
        output="",
        cited_node_ids=[],
        trace_table=resp.trace_table,
        client=client,
    )
    trace_id = CitationTracker(conn).write_response(
        record, retrieved_node_ids=[n.node_id for n in resp.nodes],
    )
    reflect_job_id = maybe_enqueue_retrieve_reflect(conn, project_id, trace_id)
    pending = pending_effects_from_reflect(reflect_job_id, trigger="retrieve")
    return RetrieveResult(response=resp, trace_id=trace_id, pending_effects=pending)
