"""FastMCP server exposing the curated loci tools.

Tools:
    loci.retrieve(project, query, k?, anchors?, hyde?)
    loci.draft(project, instruction, context_md?, style?, cite_density?, k?)
    loci.expand_citation(response_id)
    loci.expand_node(node_id)
    loci.propose_node(project, subkind, title, body, cites?, related?)
    loci.accept_proposal(proposal_id)
    loci.absorb(project)

The MCP server uses the same SQLite DB and same code paths as the REST API.
We resolve project by slug OR id so callers don't have to track ULIDs.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from loci.citations import CitationTracker
from loci.config import get_settings
from loci.db import migrate
from loci.db.connection import get_connection
from loci.draft import DraftRequest
from loci.draft import draft as run_draft
from loci.embed.local import get_embedder
from loci.graph import EdgeRepository, NodeRepository, ProjectRepository
from loci.graph.models import (
    InterpretationNode,
)
from loci.jobs import enqueue
from loci.retrieve import RetrievalRequest, Retriever

log = logging.getLogger(__name__)


def build_mcp_server() -> FastMCP:
    """Construct a FastMCP server with all loci tools registered."""
    # Migrations applied on construction so a fresh data dir works.
    settings = get_settings()
    settings.ensure_dirs()
    migrate()

    mcp = FastMCP(
        name="loci",
        instructions=(
            "loci is a personal memory graph. Use `retrieve` to find prior "
            "interpretations + raw sources for a topic, `draft` to write text "
            "with cited evidence from the user's graph, `expand_citation` to "
            "look up a previous response, and `propose_node` / "
            "`accept_proposal` for graph mutation. Always prefer a draft over "
            "raw retrieval when the user wants writing — drafts include the "
            "citation block they need."
        ),
    )

    @mcp.tool(
        name="loci_retrieve",
        description=(
            "Search a loci project for relevant interpretations and raw "
            "sources. Returns a ranked list of nodes with `why` strings "
            "explaining how they matched."
        ),
    )
    def loci_retrieve(
        project: str,
        query: str,
        k: int = 10,
        anchors: list[str] | None = None,
        hyde: bool = False,
    ) -> dict[str, Any]:
        conn = get_connection()
        proj = _resolve_project(conn, project)
        retriever = Retriever(conn)
        resp = retriever.retrieve(RetrievalRequest(
            project_id=proj.id, query=query, k=k,
            anchors=anchors or [], hyde=hyde,
        ))
        rid = CitationTracker(conn).write_response(
            __record_for(proj.id, query, k, hyde),
            retrieved_node_ids=[n.node_id for n in resp.nodes],
        )
        return {
            "nodes": [
                {
                    "id": n.node_id, "kind": n.kind, "subkind": n.subkind,
                    "title": n.title, "snippet": n.snippet, "score": n.score,
                    "why": n.why,
                }
                for n in resp.nodes
            ],
            "trace_id": rid,
        }

    @mcp.tool(
        name="loci_draft",
        description=(
            "Write a markdown draft for a project, citing nodes from the "
            "graph. Returns `output_md` plus a structured citations[] array. "
            "Pass `context_md` if the user has draft text already."
        ),
    )
    def loci_draft(
        project: str,
        instruction: str,
        context_md: str | None = None,
        anchors: list[str] | None = None,
        style: str = "prose",
        cite_density: str = "normal",
        k: int = 12,
        hyde: bool = False,
    ) -> dict[str, Any]:
        conn = get_connection()
        proj = _resolve_project(conn, project)
        result = run_draft(conn, DraftRequest(
            project_id=proj.id, session_id="mcp",
            instruction=instruction, context_md=context_md,
            anchors=anchors or [],
            style=style,  # type: ignore[arg-type]
            cite_density=cite_density,  # type: ignore[arg-type]
            k=k, hyde=hyde, client="mcp",
        ))
        return {
            "output_md": result.output_md,
            "citations": [c.__dict__ for c in result.citations],
            "response_id": result.response_id,
        }

    @mcp.tool(
        name="loci_expand_citation",
        description="Look up a previous loci response by id. Returns the original request, output, and the node ids it cited.",
    )
    def loci_expand_citation(response_id: str) -> dict[str, Any]:
        conn = get_connection()
        rec = CitationTracker(conn).get_response(response_id)
        if rec is None:
            return {"error": "response not found", "response_id": response_id}
        return rec

    @mcp.tool(
        name="loci_expand_node",
        description="Fetch a single node + its outgoing/incoming edges. Useful when a citation points at a node id.",
    )
    def loci_expand_node(node_id: str) -> dict[str, Any]:
        conn = get_connection()
        n = NodeRepository(conn).get(node_id)
        if n is None:
            return {"error": "node not found", "node_id": node_id}
        out_edges = EdgeRepository(conn).from_node(node_id)
        return {
            "node": n.model_dump(),
            "edges_out": [e.model_dump() for e in out_edges],
        }

    @mcp.tool(
        name="loci_propose_node",
        description=(
            "Propose a new interpretation node for the user's graph. The node "
            "lands as `proposed` and surfaces in the proposal queue until the "
            "user accepts or dismisses it. Use `subkind` from "
            "{philosophy, pattern, tension, decision, question, touchstone, "
            "experiment, metaphor}."
        ),
    )
    def loci_propose_node(
        project: str,
        subkind: str,
        title: str,
        body: str,
        cites: list[str] | None = None,
        related: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        conn = get_connection()
        proj = _resolve_project(conn, project)
        nodes_repo = NodeRepository(conn)
        edges_repo = EdgeRepository(conn)
        node = InterpretationNode(
            subkind=subkind,  # type: ignore[arg-type]
            title=title, body=body,
            origin="proposal_accepted",
            status="proposed",
            confidence=0.5,
        )
        emb_text = f"{title}\n\n{body}".strip()
        emb = get_embedder().encode(emb_text) if emb_text else None
        nodes_repo.create_interpretation(node, embedding=emb)
        ProjectRepository(conn).add_member(proj.id, node.id, role="included")
        for raw_id in (cites or []):
            edges_repo.create(node.id, raw_id, type="cites")
        for typ, dst_ids in (related or {}).items():
            for dst in dst_ids:
                edges_repo.create(node.id, dst, type=typ)  # type: ignore[arg-type]
        return {"node_id": node.id, "status": "proposed"}

    @mcp.tool(
        name="loci_accept_proposal",
        description="Accept a proposal by id. Promotes the proposed node to `live` and bumps confidence.",
    )
    def loci_accept_proposal(proposal_id: str) -> dict[str, Any]:
        conn = get_connection()
        row = conn.execute(
            "SELECT id, project_id, kind, payload, status FROM proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            return {"error": "proposal not found"}
        if row["status"] != "pending":
            return {"error": f"proposal not pending (status={row['status']})"}
        # Mark accepted. Side effects depend on the proposal kind.
        conn.execute(
            "UPDATE proposals SET status = 'accepted', resolved_at = datetime('now') WHERE id = ?",
            (proposal_id,),
        )
        if row["kind"] == "node":
            payload = json.loads(row["payload"])
            nid = payload.get("about_node_id")
            if nid:
                conn.execute(
                    "UPDATE nodes SET status = 'live' WHERE id = ?", (nid,)
                )
                conn.execute(
                    """UPDATE nodes SET confidence = MIN(1.0, confidence + 0.15)
                       WHERE id = ?""",
                    (nid,),
                )
        return {"proposal_id": proposal_id, "status": "accepted"}

    @mcp.tool(
        name="loci_absorb",
        description="Enqueue an absorb checkpoint job for the project. Returns a job_id; poll via the REST /jobs/:id endpoint.",
    )
    def loci_absorb(project: str) -> dict[str, Any]:
        conn = get_connection()
        proj = _resolve_project(conn, project)
        job_id = enqueue(conn, kind="absorb", project_id=proj.id)
        return {"job_id": job_id, "status": "queued"}

    return mcp


def __record_for(project_id: str, query: str, k: int, hyde: bool):
    """Build a ResponseRecord for a retrieve-only call (no output)."""
    from loci.citations import ResponseRecord
    return ResponseRecord(
        project_id=project_id, session_id="mcp",
        request={"query": query, "k": k, "hyde": hyde},
        output="", cited_node_ids=[], client="mcp",
    )


def _resolve_project(conn, project_str: str):
    repo = ProjectRepository(conn)
    proj = repo.get_by_slug(project_str) or repo.get(project_str)
    if proj is None:
        raise ValueError(f"project not found: {project_str}")
    return proj


def run_stdio() -> None:
    """Run MCP over stdio (for Claude Code, which subprocesses MCP servers)."""
    server = build_mcp_server()
    server.run(transport="stdio")
