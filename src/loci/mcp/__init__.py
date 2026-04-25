"""MCP adapter.

PLAN.md §Open questions: "MCP tool granularity. Should MCP expose every REST
endpoint or a curated subset? Probably the latter: loci.retrieve, loci.draft,
loci.expand_citation, loci.propose_node, loci.accept_proposal, and loci.absorb.
Other ops are admin and don't need to be in the agent's tool list."

We use the official `mcp` Python SDK's `FastMCP` server, which supports both
stdio (for Claude Code) and Streamable HTTP (for remote clients) transports.
"""

from loci.mcp.server import build_mcp_server, run_stdio

__all__ = ["build_mcp_server", "run_stdio"]
