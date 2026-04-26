"""FastMCP server exposing the curated loci tools.

The graph is now a directed acyclic graph: raws are leaves, interpretations
("loci of thought") are inner nodes connected by `derives_from`, and `cites`
edges run from a locus to the raw it points at. Retrieval routes the query
through loci to surface raws + a per-raw trace; drafts cite the raws (never
the loci) and ship a routing-locus side panel for the user to inspect.

Tools:
    loci_retrieve(query, project?, k?, anchors?, hyde?)
    loci_draft(instruction, project?, context_md?, style?, cite_density?, k?)
    loci_expand_citation(response_id)
    loci_expand_node(node_id)
    loci_propose_node(subkind, title, relation_md, overlap_md, source_anchor_md,
                      angle?, body?, project?, cites?, derives_from?)
    loci_accept_proposal(proposal_id)
    loci_absorb(project?)
    loci_feedback(response_id, edited_markdown)
    loci_workspace_create(slug, name, kind?, description_md?)
    loci_workspace_list()
    loci_workspace_link(workspace, project?)
    loci_workspace_unlink(workspace, project?)
    loci_workspace_add_source(workspace, root_path, label?)
    loci_current_project()
    loci_context(project?, hours?)

All tools accept an optional `project` argument. When omitted, the project is
auto-resolved via: LOCI_PROJECT env → .loci/project file walk-up from cwd.
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
from loci.graph.models import InterpretationNode, Workspace, WorkspaceKind, now_iso
from loci.graph.workspaces import WorkspaceRepository
from loci.jobs import enqueue
from loci.mcp.resolve import ProjectNotFound, resolve_project_id
from loci.retrieve import RetrievalRequest, Retriever

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# IPC bridge: publish trace_run events to the HTTP server's WS bus.
# The MCP server is a separate process; it can't reach the in-process bus
# directly. We fire-and-forget a POST to the HTTP server's broadcast endpoint.
# ---------------------------------------------------------------------------

_LOCI_API_URL = "http://127.0.0.1:7077"


def _broadcast_trace_run(project_id: str, payload: dict[str, Any]) -> None:
    """Fire-and-forget: ship the trace-run payload to the HTTP server."""
    import threading
    import urllib.request

    def _post() -> None:
        try:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                f"{_LOCI_API_URL}/projects/{project_id}/mcp/publish-trace",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=2)  # noqa: S310
        except Exception:
            pass  # best-effort; don't crash MCP on broadcast failure

    threading.Thread(target=_post, daemon=True).start()


def build_mcp_server() -> FastMCP:
    """Construct a FastMCP server with all loci tools registered."""
    # Migrations applied on construction so a fresh data dir works.
    settings = get_settings()
    settings.ensure_dirs()
    migrate()

    mcp = FastMCP(
        name="loci",
        instructions=(
            "loci is a personal memory DAG. Raws are leaves (the user's actual "
            "sources); interpretations are 'loci of thought' — pointers that "
            "say which part of which source matters and why. Use `retrieve` to "
            "route a query through loci to the raws they point at; the "
            "response includes raws, the loci that routed to them, and a "
            "trace_table. Use `draft` to write text — citations land on raws "
            "only (never on loci), with a routing_loci side panel for the user "
            "to inspect the path. Use `expand_citation` to recover a past "
            "response, `propose_node` to add a locus, and `accept_proposal` to "
            "promote it. Prefer `draft` when the user wants writing — it "
            "includes the trace and citation block they need."
        ),
    )

    @mcp.tool(
        name="loci_retrieve",
        description=(
            "Route a query through the user's loci-of-thought graph and "
            "return the raw sources those loci point at. The response has: "
            "`nodes` (ranked raws with per-node trace), `routing_loci` (the "
            "loci that routed retrieval, with relation/overlap/source_anchor — "
            "for context, NOT for citing), and `trace_table` (per-raw interp "
            "path). `project` is optional when LOCI_PROJECT is set or a "
            ".loci/project file exists."
        ),
    )
    def loci_retrieve(
        query: str,
        project: str | None = None,
        k: int = 10,
        anchors: list[str] | None = None,
        hyde: bool = False,
    ) -> dict[str, Any]:
        conn = get_connection()
        try:
            project_id = resolve_project_id(conn, project)
        except ProjectNotFound as e:
            return {"error": str(e)}
        retriever = Retriever(conn)
        resp = retriever.retrieve(RetrievalRequest(
            project_id=project_id, query=query, k=k,
            anchors=anchors or [], hyde=hyde,
        ))
        rid = CitationTracker(conn).write_response(
            __record_for(project_id, query, k, hyde, trace_table=resp.trace_table),
            retrieved_node_ids=[n.node_id for n in resp.nodes],
        )
        _broadcast_trace_run(project_id, {
            "response_id": rid,
            "session_id": "mcp",
            "query": query,
            "ts": now_iso(),
            "k": k,
            "routing_loci": [
                {
                    "id": ri.node_id, "subkind": ri.subkind, "title": ri.title,
                    "relation_md": ri.relation_md, "overlap_md": ri.overlap_md,
                    "source_anchor_md": ri.source_anchor_md,
                    "angle": ri.angle, "score": ri.score,
                }
                for ri in resp.routing_interps
            ],
            "trace_table": resp.trace_table,
        })
        # Enqueue a lightweight reflect if the project hasn't reflected recently.
        _maybe_enqueue_reflect(conn, project_id, rid)
        return {
            "nodes": [
                {
                    "id": n.node_id, "kind": n.kind, "subkind": n.subkind,
                    "title": n.title, "snippet": n.snippet, "score": n.score,
                    "why": n.why,
                    "trace": [
                        {"id": h.src, "edge": h.edge_type, "to": h.dst}
                        for h in n.trace
                    ],
                }
                for n in resp.nodes
            ],
            "routing_loci": [
                {
                    "id": ri.node_id, "subkind": ri.subkind, "title": ri.title,
                    "relation_md": ri.relation_md, "overlap_md": ri.overlap_md,
                    "source_anchor_md": ri.source_anchor_md,
                    "angle": ri.angle, "score": ri.score,
                }
                for ri in resp.routing_interps
            ],
            "trace_table": resp.trace_table,
            "trace_id": rid,
        }

    @mcp.tool(
        name="loci_draft",
        description=(
            "Write a markdown draft for a project. Citations land on RAW "
            "sources only — loci of thought are surfaced separately as "
            "routing context, not as citable content. Returns `output_md`, "
            "`citations` (raws with their routing trace), `routing_loci` "
            "(loci that pointed at the cited raws), and `trace_table` "
            "(per-raw interp path). Pass `context_md` if the user has draft "
            "text already."
        ),
    )
    def loci_draft(
        instruction: str,
        project: str | None = None,
        context_md: str | None = None,
        anchors: list[str] | None = None,
        style: str = "prose",
        cite_density: str = "normal",
        k: int = 12,
        hyde: bool = False,
    ) -> dict[str, Any]:
        conn = get_connection()
        try:
            project_id = resolve_project_id(conn, project)
        except ProjectNotFound as e:
            return {"error": str(e)}
        result = run_draft(conn, DraftRequest(
            project_id=project_id, session_id="mcp",
            instruction=instruction, context_md=context_md,
            anchors=anchors or [],
            style=style,  # type: ignore[arg-type]
            cite_density=cite_density,  # type: ignore[arg-type]
            k=k, hyde=hyde, client="mcp",
        ))
        _routing_loci_dicts = [
            {
                "id": rl.node_id, "subkind": rl.subkind, "title": rl.title,
                "relation_md": rl.relation_md, "overlap_md": rl.overlap_md,
                "source_anchor_md": rl.source_anchor_md,
                "angle": rl.angle, "score": rl.score,
            }
            for rl in result.routing_loci
        ]
        _broadcast_trace_run(project_id, {
            "response_id": result.response_id,
            "session_id": "mcp",
            "query": instruction,
            "ts": now_iso(),
            "k": k,
            "routing_loci": _routing_loci_dicts,
            "trace_table": result.trace_table,
        })
        return {
            "output_md": result.output_md,
            "citations": [
                {
                    "node_id": c.node_id, "kind": c.kind, "subkind": c.subkind,
                    "title": c.title, "why_cited": c.why_cited,
                    "routed_by": c.routed_by,
                }
                for c in result.citations
            ],
            "routing_loci": _routing_loci_dicts,
            "trace_table": result.trace_table,
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
            "Propose a new locus of thought (interpretation node) for the "
            "user's graph. A locus must have the three slots: relation_md "
            "(how the source(s) relate to the project), overlap_md (the "
            "concrete intersection), source_anchor_md (which part of which "
            "source carries the weight). The locus lands as `proposed` until "
            "the user accepts or dismisses it. `subkind` ∈ {tension, "
            "decision, philosophy, relevance}; for `relevance` set `angle` "
            "from the closed vocabulary. `cites` lists raw node ids the "
            "locus points at; `derives_from` lists upstream loci this one "
            "builds on."
        ),
    )
    def loci_propose_node(
        subkind: str,
        title: str,
        relation_md: str,
        overlap_md: str,
        source_anchor_md: str,
        body: str = "",
        angle: str | None = None,
        project: str | None = None,
        cites: list[str] | None = None,
        derives_from: list[str] | None = None,
    ) -> dict[str, Any]:
        conn = get_connection()
        try:
            project_id = resolve_project_id(conn, project)
        except ProjectNotFound as e:
            return {"error": str(e)}
        from loci.graph.edges import EdgeError

        nodes_repo = NodeRepository(conn)
        edges_repo = EdgeRepository(conn)
        node = InterpretationNode(
            subkind=subkind,  # type: ignore[arg-type]
            title=title, body=body,
            relation_md=relation_md, overlap_md=overlap_md,
            source_anchor_md=source_anchor_md,
            angle=angle,  # type: ignore[arg-type]
            origin="proposal_accepted",
            status="proposed",
            confidence=0.5,
        )
        emb_text = "\n\n".join(p for p in [
            title, relation_md, overlap_md, source_anchor_md,
        ] if p).strip()
        emb = get_embedder().encode(emb_text) if emb_text else None
        nodes_repo.create_interpretation(node, embedding=emb)
        ProjectRepository(conn).add_member(project_id, node.id, role="included")

        edge_errors: list[str] = []
        for raw_id in (cites or []):
            try:
                edges_repo.create(node.id, raw_id, type="cites")
            except EdgeError as exc:
                edge_errors.append(f"cites→{raw_id}: {exc}")
        for upstream in (derives_from or []):
            try:
                edges_repo.create(node.id, upstream, type="derives_from")
            except EdgeError as exc:
                edge_errors.append(f"derives_from→{upstream}: {exc}")
        result = {"node_id": node.id, "status": "proposed"}
        if edge_errors:
            result["edge_errors"] = edge_errors
        return result

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
    def loci_absorb(project: str | None = None) -> dict[str, Any]:
        conn = get_connection()
        try:
            project_id = resolve_project_id(conn, project)
        except ProjectNotFound as e:
            return {"error": str(e)}
        job_id = enqueue(conn, kind="absorb", project_id=project_id)
        return {"job_id": job_id, "status": "queued"}

    @mcp.tool(
        name="loci_feedback",
        description=(
            "Submit feedback on a previous loci response. Pass the response_id "
            "from a prior retrieve/draft call and your edited markdown. This "
            "queues a reflect job so the graph learns from your edits."
        ),
    )
    def loci_feedback(
        response_id: str,
        edited_markdown: str,
    ) -> dict[str, Any]:
        conn = get_connection()
        rec = CitationTracker(conn).get_response(response_id)
        if rec is None:
            return {"error": "response not found", "response_id": response_id}
        project_id = rec.get("project_id")
        if not project_id:
            return {"error": "response has no project_id"}
        # Write the edited text as a follow-up response so the reflect cycle
        # can diff against the original and learn citation feedback.
        from loci.citations import ResponseRecord
        follow_up = ResponseRecord(
            project_id=project_id, session_id="mcp",
            request={"edited_from": response_id},
            output=edited_markdown,
            cited_node_ids=rec.get("cited_node_ids", []),
            client="mcp_feedback",
        )
        frid = CitationTracker(conn).write_response(follow_up, retrieved_node_ids=[])
        job_id = enqueue(
            conn, kind="reflect", project_id=project_id,
            payload={"response_id": frid, "trigger": "user_feedback"},
        )
        return {"feedback_response_id": frid, "reflect_job_id": job_id, "status": "queued"}

    @mcp.tool(
        name="loci_current_project",
        description=(
            "Return the project that would be auto-resolved for the current "
            "working directory. Useful for confirming which project loci tools "
            "will target when `project` is omitted."
        ),
    )
    def loci_current_project() -> dict[str, Any]:
        conn = get_connection()
        try:
            project_id = resolve_project_id(conn)
        except ProjectNotFound as e:
            return {"error": str(e), "resolved": False}
        proj = ProjectRepository(conn).get(project_id)
        if proj is None:
            return {"error": "resolved id not found", "resolved": False}
        return {
            "resolved": True,
            "id": proj.id,
            "slug": proj.slug,
            "name": proj.name,
        }

    @mcp.tool(
        name="loci_context",
        description=(
            "Return full situational context for the current project: project info, "
            "linked information workspaces, graph stats, recently accessed nodes, and "
            "interpretation nodes created or updated in the last N hours. Call this at "
            "the start of a session to understand what knowledge is available and what "
            "has changed recently. `hours` controls the recency window for updated nodes "
            "(default 24)."
        ),
    )
    def loci_context(
        project: str | None = None,
        hours: int = 24,
    ) -> dict[str, Any]:
        conn = get_connection()
        try:
            project_id = resolve_project_id(conn, project)
        except ProjectNotFound as e:
            return {"error": str(e)}
        proj = ProjectRepository(conn).get(project_id)
        if proj is None:
            return {"error": "project not found"}

        ws_repo = WorkspaceRepository(conn)
        links = ws_repo.linked_workspaces(project_id)
        workspaces = []
        for ws, link in links:
            if link.role == "excluded":
                continue
            raw_count = conn.execute(
                "SELECT COUNT(*) FROM nodes n JOIN workspace_membership wm ON wm.node_id = n.id "
                "WHERE wm.workspace_id = ? AND n.kind = 'raw'",
                (ws.id,),
            ).fetchone()[0]
            workspaces.append({
                "id": ws.id, "slug": ws.slug, "name": ws.name,
                "kind": ws.kind, "role": link.role,
                "raw_count": raw_count,
                "description_md": ws.description_md,
            })

        stats_row = conn.execute(
            """
            SELECT
                SUM(CASE n.kind WHEN 'raw' THEN 1 ELSE 0 END) AS raw_nodes,
                SUM(CASE n.kind WHEN 'interpretation' THEN 1 ELSE 0 END) AS interpretation_nodes,
                SUM(CASE n.status WHEN 'live' THEN 1 ELSE 0 END) AS live_nodes
            FROM nodes n
            JOIN project_effective_members pm ON pm.node_id = n.id
            WHERE pm.project_id = ?
            """,
            (project_id,),
        ).fetchone()

        recent_accessed = conn.execute(
            """
            SELECT n.id, n.title, n.kind, n.subkind, n.last_accessed_at, n.confidence
            FROM nodes n
            JOIN project_effective_members pm ON pm.node_id = n.id
            WHERE pm.project_id = ? AND n.last_accessed_at IS NOT NULL
            ORDER BY n.last_accessed_at DESC LIMIT 8
            """,
            (project_id,),
        ).fetchall()

        from datetime import UTC, datetime, timedelta
        since = (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        recent_nodes = conn.execute(
            """
            SELECT n.id, n.kind, n.subkind, n.title, n.body, n.confidence, n.status,
                   n.created_at, n.updated_at
            FROM nodes n
            JOIN project_effective_members pm ON pm.node_id = n.id
            WHERE pm.project_id = ? AND n.kind = 'interpretation'
              AND (n.created_at >= ? OR n.updated_at >= ?)
            ORDER BY n.updated_at DESC LIMIT 20
            """,
            (project_id, since, since),
        ).fetchall()

        return {
            "project": {
                "id": proj.id, "slug": proj.slug, "name": proj.name,
                "last_active_at": proj.last_active_at,
            },
            "workspaces": workspaces,
            "stats": {
                "raw_nodes": stats_row["raw_nodes"] or 0,
                "interpretation_nodes": stats_row["interpretation_nodes"] or 0,
                "live_nodes": stats_row["live_nodes"] or 0,
            },
            "recent_activity": [
                {"id": r["id"], "title": r["title"], "kind": r["kind"],
                 "subkind": r["subkind"], "last_accessed_at": r["last_accessed_at"],
                 "confidence": r["confidence"]}
                for r in recent_accessed
            ],
            "recently_updated": [
                {"id": r["id"], "kind": r["kind"], "subkind": r["subkind"],
                 "title": r["title"], "body": (r["body"] or "")[:300],
                 "confidence": r["confidence"], "status": r["status"],
                 "created_at": r["created_at"], "updated_at": r["updated_at"]}
                for r in recent_nodes
            ],
            "since": since,
        }

    @mcp.tool(
        name="loci_workspace_create",
        description="Create a new information workspace (a labeled bag of source roots).",
    )
    def loci_workspace_create(
        slug: str,
        name: str,
        kind: str = "mixed",
        description_md: str = "",
    ) -> dict[str, Any]:
        conn = get_connection()
        ws = Workspace(slug=slug, name=name, kind=kind, description_md=description_md)  # type: ignore[arg-type]
        WorkspaceRepository(conn).create(ws)
        return {"workspace_id": ws.id, "slug": ws.slug}

    @mcp.tool(
        name="loci_workspace_list",
        description="List all information workspaces.",
    )
    def loci_workspace_list() -> dict[str, Any]:
        conn = get_connection()
        ws_repo = WorkspaceRepository(conn)
        workspaces = ws_repo.list()
        return {
            "workspaces": [
                {"id": ws.id, "slug": ws.slug, "name": ws.name, "kind": ws.kind}
                for ws in workspaces
            ]
        }

    @mcp.tool(
        name="loci_workspace_link",
        description=(
            "Link an information workspace to a project. Enqueues a relevance "
            "pass so the graph gains typed bridge interpretations. "
            "`workspace` is the workspace slug or id."
        ),
    )
    def loci_workspace_link(
        workspace: str,
        project: str | None = None,
        role: str = "reference",
    ) -> dict[str, Any]:
        conn = get_connection()
        try:
            project_id = resolve_project_id(conn, project)
        except ProjectNotFound as e:
            return {"error": str(e)}
        ws_repo = WorkspaceRepository(conn)
        ws = ws_repo.get_by_slug(workspace) or ws_repo.get(workspace)
        if ws is None:
            return {"error": f"workspace not found: {workspace}"}
        ws_repo.link_project(project_id, ws.id, role=role)  # type: ignore[arg-type]
        job_id = enqueue(conn, kind="relevance", project_id=project_id,
                         payload={"workspace_id": ws.id, "scope": "link"})
        return {"workspace_id": ws.id, "project_id": project_id,
                "role": role, "relevance_job_id": job_id}

    @mcp.tool(
        name="loci_workspace_unlink",
        description=(
            "Unlink a workspace from a project. Queues sweep_orphans to mark "
            "interpretations that depended on that workspace's sources as dirty."
        ),
    )
    def loci_workspace_unlink(
        workspace: str,
        project: str | None = None,
    ) -> dict[str, Any]:
        conn = get_connection()
        try:
            project_id = resolve_project_id(conn, project)
        except ProjectNotFound as e:
            return {"error": str(e)}
        ws_repo = WorkspaceRepository(conn)
        ws = ws_repo.get_by_slug(workspace) or ws_repo.get(workspace)
        if ws is None:
            return {"error": f"workspace not found: {workspace}"}
        ws_repo.unlink_project(project_id, ws.id)
        job_id = enqueue(conn, kind="sweep_orphans", project_id=project_id,
                         payload={"workspace_id": ws.id})
        return {"workspace_id": ws.id, "project_id": project_id,
                "sweep_job_id": job_id}

    @mcp.tool(
        name="loci_workspace_add_source",
        description="Register a root directory as a source for a workspace.",
    )
    def loci_workspace_add_source(
        workspace: str,
        root_path: str,
        label: str | None = None,
    ) -> dict[str, Any]:
        conn = get_connection()
        from pathlib import Path
        ws_repo = WorkspaceRepository(conn)
        ws = ws_repo.get_by_slug(workspace) or ws_repo.get(workspace)
        if ws is None:
            return {"error": f"workspace not found: {workspace}"}
        ws_src = ws_repo.add_source(ws.id, Path(root_path), label=label)
        return {"source_id": ws_src.id, "workspace_id": ws.id, "root_path": root_path}

    @mcp.tool(
        name="loci_edit_locus",
        description=(
            "Edit a locus of thought's belief slots directly — relation_md, "
            "overlap_md, source_anchor_md, and/or angle. The node is re-embedded "
            "and the change is published immediately. Only provide the slots you "
            "want to change; omitted slots are preserved."
        ),
    )
    def loci_edit_locus(
        node_id: str,
        relation_md: str | None = None,
        overlap_md: str | None = None,
        source_anchor_md: str | None = None,
        angle: str | None = None,
    ) -> dict[str, Any]:
        conn = get_connection()
        n = NodeRepository(conn).get(node_id)
        if n is None:
            return {"error": "node not found", "node_id": node_id}
        if n.kind != "interpretation":
            return {"error": "loci_edit_locus only applies to interpretation nodes"}
        rel = relation_md if relation_md is not None else getattr(n, "relation_md", "")
        ovl = overlap_md if overlap_md is not None else getattr(n, "overlap_md", "")
        anc = source_anchor_md if source_anchor_md is not None else getattr(n, "source_anchor_md", "")
        emb_text = "\n\n".join(p for p in [n.title, rel, ovl, anc] if p).strip()
        new_emb = get_embedder().encode(emb_text) if emb_text else None
        NodeRepository(conn).update_locus(
            node_id,
            relation_md=relation_md, overlap_md=overlap_md,
            source_anchor_md=source_anchor_md, angle=angle,
            new_embedding=new_emb,
        )
        from loci.api.publishers import publish_node_upsert
        updated = NodeRepository(conn).get(node_id)
        if updated is not None:
            publish_node_upsert(conn, updated)
        return {"updated": True, "node_id": node_id}

    @mcp.tool(
        name="loci_add_citation",
        description="Add a `cites` edge from a locus of thought to a raw source node.",
    )
    def loci_add_citation(locus_id: str, raw_id: str) -> dict[str, Any]:
        conn = get_connection()
        from loci.graph.edges import EdgeError
        from loci.api.publishers import publish_edge_upsert
        try:
            edge = EdgeRepository(conn).create(locus_id, raw_id, type="cites")
        except EdgeError as exc:
            return {"error": str(exc)}
        publish_edge_upsert(conn, edge)
        return {"edge_id": edge.id, "src": locus_id, "dst": raw_id}

    @mcp.tool(
        name="loci_remove_citation",
        description="Delete a `cites` edge by edge id (remove a citation from a locus to a raw source).",
    )
    def loci_remove_citation(edge_id: str) -> dict[str, Any]:
        conn = get_connection()
        from loci.api.publishers import projects_for_edge, publish_edge_delete
        repo = EdgeRepository(conn)
        existing = repo.get(edge_id)
        if existing is None:
            return {"error": "edge not found", "edge_id": edge_id}
        src, dst = existing.src, existing.dst
        pids = projects_for_edge(conn, src, dst)
        repo.delete(edge_id)
        publish_edge_delete(conn, edge_id, src=src, dst=dst, project_ids=pids)
        return {"deleted": True, "edge_id": edge_id}

    @mcp.tool(
        name="loci_delete_node",
        description=(
            "Hard-delete an interpretation node and all its incident edges. "
            "Permanent — cannot be undone. "
            "Use `loci_remove_citation` to delete a single citation edge instead."
        ),
    )
    def loci_delete_node(node_id: str) -> dict[str, Any]:
        conn = get_connection()
        from loci.api.publishers import (
            projects_for_edge,
            projects_for_node,
            publish_edge_delete,
            publish_node_delete,
        )
        n = NodeRepository(conn).get(node_id)
        if n is None:
            return {"error": "node not found", "node_id": node_id}
        if n.kind == "raw":
            return {"error": "raw nodes cannot be deleted via this tool"}
        edge_rows = conn.execute(
            "SELECT id, src, dst FROM edges WHERE src = ? OR dst = ?", (node_id, node_id)
        ).fetchall()
        edge_fan = [
            (r["id"], r["src"], r["dst"], projects_for_edge(conn, r["src"], r["dst"]))
            for r in edge_rows
        ]
        node_project_ids = projects_for_node(conn, node_id)
        NodeRepository(conn).hard_delete(node_id)
        for eid, src, dst, pids in edge_fan:
            publish_edge_delete(conn, eid, src=src, dst=dst, project_ids=pids)
        publish_node_delete(conn, node_id, project_ids=node_project_ids)
        return {"deleted": True, "node_id": node_id}

    return mcp


def __record_for(
    project_id: str, query: str, k: int, hyde: bool,
    *, trace_table: list[dict] | None = None,
):
    """Build a ResponseRecord for a retrieve-only call (no output)."""
    from loci.citations import ResponseRecord
    return ResponseRecord(
        project_id=project_id, session_id="mcp",
        request={"query": query, "k": k, "hyde": hyde},
        output="", cited_node_ids=[],
        trace_table=trace_table or [],
        client="mcp",
    )


_REFLECT_COOLDOWN_SECONDS = 300  # 5 minutes


def _maybe_enqueue_reflect(conn, project_id: str, response_id: str) -> None:
    """Enqueue a lightweight reflect after retrieve, with cooldown."""
    last = conn.execute(
        "SELECT MAX(ts) FROM agent_reflections WHERE project_id = ?",
        (project_id,),
    ).fetchone()[0]
    if last:
        from datetime import UTC, datetime
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            elapsed = (datetime.now(UTC) - last_dt).total_seconds()
            if elapsed < _REFLECT_COOLDOWN_SECONDS:
                return
        except Exception:  # noqa: BLE001
            pass
    enqueue(conn, kind="reflect", project_id=project_id,
            payload={"response_id": response_id, "trigger": "retrieve", "lightweight": True})


def run_stdio() -> None:
    """Run MCP over stdio (for Claude Code, which subprocesses MCP servers)."""
    server = build_mcp_server()
    server.run(transport="stdio")
