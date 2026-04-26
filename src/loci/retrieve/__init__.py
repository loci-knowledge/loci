"""Retrieval pipeline — interpretation-routed.

The new model:

    Interpretations are LOCI OF THOUGHT (routers), not retrieval targets.
    A query reaches raws via the loci that point at them.

Stages (see `pipeline.Retriever.retrieve` for the recipe):

    1. INTERP STAGE   — lex + vec + (HyDE) + PPR over the derives_from DAG,
                         RRF-fused → top-K_interp routing loci.
    2. ROUTE STAGE    — walk cites and derives_from·cites from the top loci
                         to raws, accumulating a per-raw provenance trace.
    3. DIRECT STAGE   — also score raws directly so we don't miss raws that
                         match the query without a routing locus.
    4. MERGE          — direct score + routed bonus (capped) → final raws.
    5. RESPONSE       — `nodes` (raws), `routing_interps` (the loci used as
                         routers), `trace_table` (per-raw interp path).

The shape of the result is documented in `pipeline.RetrievedNode` and
`pipeline.RetrievalResponse`.
"""

from loci.retrieve.pipeline import (
    RetrievalRequest,
    RetrievalResponse,
    RetrievedNode,
    Retriever,
    RouteHop,
    RoutingInterp,
)

__all__ = [
    "RetrievalRequest",
    "RetrievalResponse",
    "RetrievedNode",
    "Retriever",
    "RouteHop",
    "RoutingInterp",
]
