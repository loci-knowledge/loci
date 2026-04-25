"""Citation contract: trace + response persistence.

PLAN.md §Citations: every retrieve/draft call writes a `Response` row and a
`Trace` row per cited node. The expansion endpoints (`GET /responses/:id`,
`GET /nodes/:id/trace`, `GET /nodes/:id/responses`) become trivial joins.

The orchestrator in `loci/api/routes/{retrieve,draft}.py` calls into this
module after the retrieval/draft completes.
"""

from loci.citations.tracker import (
    CitationTracker,
    ResponseRecord,
    TraceKind,
    write_response_with_traces,
)

__all__ = [
    "CitationTracker",
    "ResponseRecord",
    "TraceKind",
    "write_response_with_traces",
]
