"""Resolve where autoresearch artifacts should be written.

Preference:
  1. `<repo>/.loci/research/<run_id>/` when a per-repo .loci/ dir exists.
  2. `~/.loci/research/<run_id>/` otherwise.

In both cases the chosen directory becomes a synthetic source root that is
added to the workspace, so the artifacts are ingested as raw nodes.
"""

from __future__ import annotations

from pathlib import Path


def resolve_research_output_dir(run_id: str) -> Path:
    """Return the output directory for an autoresearch run.

    Uses the per-repo .loci/ dir when available, falls back to ~/.loci/research/.
    """
    from loci.config import get_settings
    from loci.mcp.resolve import find_project_dir

    project_dir = find_project_dir()
    if project_dir is not None:
        # Write next to the per-repo .loci/ — git-trackable if desired.
        out = project_dir / "research" / run_id
    else:
        # Fallback: central user data dir.
        out = get_settings().research_dir / run_id

    out.mkdir(parents=True, exist_ok=True)
    return out


def resolve_research_source_root(run_id: str) -> Path:
    """Return the parent directory (research/) used as a workspace source root.

    This is what gets added to workspace_sources so scan_workspace can ingest
    autoresearch artifacts.
    """
    from loci.config import get_settings
    from loci.mcp.resolve import find_project_dir

    project_dir = find_project_dir()
    if project_dir is not None:
        return project_dir / "research"
    return get_settings().research_dir
