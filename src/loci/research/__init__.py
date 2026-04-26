"""Auto-research module.

A research sub-agent that searches papers (HF Papers + Semantic Scholar +
arXiv), runs code in an HF Spaces sandbox, and produces artifacts that flow
back into loci's graph as raw nodes plus a routing locus.

Public API:
    from loci.research import run_research

The job kind `autoresearch` (in `loci.jobs.autoresearch`) wraps this for
async execution via the existing job queue.
"""

from loci.research.agent import ResearchReport, run_research

__all__ = ["ResearchReport", "run_research"]
