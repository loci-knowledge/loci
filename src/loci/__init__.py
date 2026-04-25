"""loci — a personal memory graph server.

See PLAN.md at the repo root for the design. The package is organised by
concern, not by layer: each subpackage owns one responsibility end-to-end
(`graph` owns the node/edge model; `ingest` owns walking + hashing + extracting;
`retrieve` owns lex+vec+ppr fusion; etc).

Stack choices (April 2026):
- sqlite-vec for vector ANN co-located with the graph (single .sqlite file).
- sentence-transformers + BAAI/bge-small-en-v1.5 (384-d) as the local default.
- FastAPI + websockets for HTTP/WS; FastMCP (mcp>=1.x) for MCP stdio + Streamable HTTP.
- scipy.sparse for Personalized PageRank (CSR power iteration).
- Anthropic SDK for drafting + classification.
- pypdf for PDFs (BSD); pymupdf4llm available via `loci[pdf-quality]` for higher
  quality (AGPL).
"""

__version__ = "0.1.0"
