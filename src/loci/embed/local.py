"""Local embedding model wrapper.

Single class (`Embedder`) wrapping `sentence_transformers.SentenceTransformer`.
The wrapper exists for three reasons:

1. **Lazy load.** Importing torch + transformers is ~3s and ~1 GB of RAM. We
   don't want to pay that on `loci --help`. The model is loaded on first
   `encode*` call.

2. **Device auto-selection.** `device='auto'` resolves to MPS on Apple Silicon
   (transformers ≥ 4.x supports MPS as a first-class backend), CUDA if
   available, else CPU. Users can pin via `LOCI_EMBEDDING_DEVICE`.

3. **Unit-normalize on output.** sqlite-vec's default distance is L2, and our
   retrieve fusion assumes unit-norm vectors so L2 ↔ cosine is monotonic. We
   set `normalize_embeddings=True` at encode time.

The class is process-global (one instance, accessed via `get_embedder()`)
because loading the model multiple times would defeat the lazy-load. Tests
that need fresh state should monkeypatch `_INSTANCE`.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

import numpy as np

from loci.config import get_settings

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

log = logging.getLogger(__name__)

_INSTANCE: Embedder | None = None
_LOCK = threading.Lock()


class Embedder:
    """Encode text → unit-normalized float32 vectors.

    Thread-safe: SentenceTransformer's encode() releases the GIL during the
    forward pass, so concurrent encode() calls from FastAPI workers are fine.
    Model load itself is lock-protected by `get_embedder()`.
    """

    def __init__(self, model_name: str, device: str, batch_size: int) -> None:
        self.model_name = model_name
        self.device = device
        self.batch_size = batch_size
        self._model: SentenceTransformer | None = None

    @property
    def dim(self) -> int:
        """The embedding dimension. Loads the model if necessary."""
        return self._load().get_sentence_embedding_dimension()

    def _load(self) -> SentenceTransformer:
        if self._model is not None:
            return self._model
        # Resolve device. We import torch only here to keep the cold path lean.
        from sentence_transformers import SentenceTransformer

        device = self._resolve_device(self.device)
        settings = get_settings()
        log.info(
            "Loading embedding model %s on device=%s (cache=%s)",
            self.model_name, device, settings.model_cache_dir,
        )
        self._model = SentenceTransformer(
            self.model_name,
            device=device,
            cache_folder=str(settings.model_cache_dir),
        )
        return self._model

    @staticmethod
    def _resolve_device(requested: str) -> str:
        """Resolve `auto` to the best available device on this machine."""
        if requested != "auto":
            return requested
        # Lazy import torch to keep startup cheap.
        import torch

        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
        return "cpu"

    def encode(self, text: str) -> np.ndarray:
        """Encode a single string. Returns shape (dim,), float32, unit-norm."""
        return self.encode_batch([text])[0]

    def encode_batch(self, texts: list[str]) -> np.ndarray:
        """Encode a list of strings. Returns shape (n, dim), float32, unit-norm.

        Empty inputs raise — silently returning a zero vector would corrupt
        downstream cosine-distance ranking. Callers should filter empty bodies
        before calling.
        """
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        if any(not t.strip() for t in texts):
            raise ValueError("encode_batch received an empty/whitespace text")
        model = self._load()
        # convert_to_numpy=True returns numpy directly; avoids a torch->numpy copy.
        # normalize_embeddings=True L2-normalises so cosine ≡ 1 - L2²/2.
        vecs = model.encode(
            texts,
            batch_size=self.batch_size,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        # SentenceTransformer can return float64 in some configs; sqlite-vec
        # wants float32 for FLOAT[N] tables.
        if vecs.dtype != np.float32:
            vecs = vecs.astype(np.float32)
        return vecs


def get_embedder() -> Embedder:
    """Return the process-global Embedder, constructing it on first call."""
    global _INSTANCE
    if _INSTANCE is not None:
        return _INSTANCE
    with _LOCK:
        if _INSTANCE is None:
            settings = get_settings()
            _INSTANCE = Embedder(
                model_name=settings.embedding_model,
                device=settings.embedding_device,
                batch_size=settings.embedding_batch_size,
            )
    return _INSTANCE


def reset_embedder() -> None:
    """For tests: clear the cached instance so the next call re-reads settings."""
    global _INSTANCE
    _INSTANCE = None


def vec_to_blob(vec: np.ndarray) -> bytes:
    """Pack a 1-D float32 vector into the byte format sqlite-vec expects.

    sqlite-vec's FLOAT[N] columns accept either a JSON-serialised list or a
    little-endian packed-float blob. The blob path is ~10× faster on insert
    and avoids string parsing on read. We always use the blob path.
    """
    if vec.dtype != np.float32:
        vec = vec.astype(np.float32)
    if not vec.flags["C_CONTIGUOUS"]:
        vec = np.ascontiguousarray(vec)
    return vec.tobytes()


def blob_to_vec(blob: bytes, dim: int) -> np.ndarray:
    """Inverse of vec_to_blob. Returns a float32 view of the bytes."""
    return np.frombuffer(blob, dtype=np.float32, count=dim).copy()
