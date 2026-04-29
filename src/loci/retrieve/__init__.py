"""Concept-graph-driven retrieval pipeline.

The new model: queries hit raw chunks directly. The concept graph (aspect
labels + co_aspect / cites edges) expands and reranks the results.

Pipeline (see `pipeline.retrieve` for the full recipe):

    1. expand_query_aspects()  → relevant aspect labels from the concept graph
    2. HyDE                    → hypothetical document embedding
    3. search_lex / search_vec → BM25 + ANN over chunks_fts / chunk_vec
    4. RRF fusion              → merged chunk ranking
    5. Graph rerank            → boost resources linked by co_aspect / cites
    6. Group + materialise     → RetrievalResult list with why_surfaced strings

Public API shapes are in `pipeline.RetrievalResult` and `pipeline.ChunkResult`.
"""

from loci.retrieve.pipeline import ChunkResult, RetrievalResult, retrieve

__all__ = [
    "ChunkResult",
    "RetrievalResult",
    "retrieve",
]
