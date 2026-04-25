"""HTTP/WS API.

The REST surface mirrors PLAN.md §API. The MCP adapter (loci/mcp/) wraps a
curated subset of the same endpoints as MCP tools so Claude Code and other
MCP clients hit the same code paths as the CLI.
"""

from loci.api.app import create_app

__all__ = ["create_app"]
