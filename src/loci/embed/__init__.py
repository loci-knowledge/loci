"""Local embedding service.

PLAN.md commits to a local model. The default is `BAAI/bge-small-en-v1.5`
(384-dim), small enough to run on CPU and accurate enough for English+code+
technical text. The model and its dimension are part of the schema (the
sqlite-vec table is created with `FLOAT[384]`); swapping models is a migration.

Why this layer exists at all:

- We don't want to import torch on the module-load path. The model is loaded
  lazily on first encode.
- Embeddings must be unit-normalized so sqlite-vec L2 distance and cosine
  similarity become monotonically related (`||a-b||² = 2 - 2·cos(a,b)` for
  unit-norm vectors). The retrieve layer assumes this.
- Batching is the one thing that matters for throughput on the ingest path.
  We batch at `embedding_batch_size` (default 32; larger on GPU).
"""

from loci.embed.local import Embedder, get_embedder

__all__ = ["Embedder", "get_embedder"]
