"""Retrieval pipeline — lex + vec + HyDE + Personalized PageRank.

The composition is:

    1. Lexical search       (FTS5 BM25 against nodes_fts)
    2. Vector search        (sqlite-vec ANN against node_vec)
    3. HyDE search          (LLM-generated hypothetical answer → embed → vec ANN)
    4. Anchor selection     (caller-provided OR project pinned + top-k vec)
    5. Personalised PageRank (sparse, seeded by anchors, over the interp graph)
    6. Reciprocal-rank fusion of (lex, vec, hyde, PPR) → final ranking
    7. Project membership + status filter
    8. `why` string assembly from the matching channels

Each step is its own module so the orchestration in `pipeline.py` reads as a
recipe. The shape of the result is documented in `pipeline.RetrievedNode`.
"""

from loci.retrieve.pipeline import RetrievalRequest, RetrievedNode, Retriever

__all__ = ["RetrievalRequest", "RetrievedNode", "Retriever"]
