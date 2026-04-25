"""Runtime settings for loci.

All settings are sourced (in order) from:

1. Environment variables prefixed `LOCI_` (e.g. `LOCI_DATA_DIR=/tmp/loci`).
2. A `.env` file in the working directory.
3. The defaults below.

The single source of truth is the `Settings` instance returned by `get_settings()`.
We expose it as a function (not a module-level constant) so tests can override
paths via `monkeypatch.setenv` and re-create the instance.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LOCI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Storage paths ---------------------------------------------------
    # data_dir holds the SQLite database, raw blob store, and any caches.
    # Default `~/.loci`. Override with `LOCI_DATA_DIR=/path`.
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".loci")

    # --- Embedding -------------------------------------------------------
    # Default to BAAI/bge-small-en-v1.5 (384-dim, ~130 MB, runs CPU + MPS on
    # Apple Silicon at thousands of tokens/s). PLAN.md commits to a local model
    # and to incremental re-embedding on the dirty/edit path; both are easier
    # with a small model. Override via `LOCI_EMBEDDING_MODEL`.
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    # Use Apple Metal Performance Shaders when available; falls back to CPU.
    embedding_device: str = "auto"  # "auto" | "cpu" | "mps" | "cuda"
    embedding_batch_size: int = 32

    # --- Retrieval -------------------------------------------------------
    # Default top-k for the retrieval pipeline. Endpoints can override.
    retrieve_default_k: int = 10
    # Personalized PageRank damping factor (Page-Brin classical 0.85).
    ppr_alpha: float = 0.85
    # Iteration cap for the sparse PPR power method. Convergence is checked
    # against L1 < ppr_tol; this is just a safety net.
    ppr_max_iter: int = 50
    ppr_tol: float = 1e-6

    # --- LLM provider keys ----------------------------------------------
    # Each key is read from the standard provider env var (no LOCI_ prefix)
    # so users don't have to set two variables. SecretStr ensures the value
    # never appears in repr/log output.
    anthropic_api_key: SecretStr | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    openrouter_api_key: SecretStr | None = Field(default=None, alias="OPENROUTER_API_KEY")

    # --- LLM model selection (per-task) ---------------------------------
    # Each spec is `<provider>:<model_name>`. Providers: anthropic, openai,
    # openrouter. The four roles below are the only ones loci uses; tasks that
    # don't need an LLM (lex, vec, PPR, ingest) ignore these entirely.
    #
    # Defaults are conservative — strong models for the writing/maintenance
    # paths, fast models for high-frequency classification.

    # Used by the absorb pipeline's contradiction pass and the kickoff job.
    # Maintains/regenerates interpretation nodes; sees the project profile
    # plus a sample of raw nodes. Wants strong reasoning + long context.
    interpretation_model: str = "openrouter:google/gemini-3-flash-preview"

    # Used by `loci draft` to synthesise output_md from retrieved candidates
    # while honouring the [Cn] citation contract. Wants strong instruction
    # following and prompt-cache friendliness.
    rag_model: str = "openrouter:google/gemini-3-flash-preview"

    # Used by the contradiction 3-way classifier (raw vs interpretation).
    # Many small calls; favour cheap + fast.
    classifier_model: str = "openrouter:deepseek/deepseek-v4-flash"

    # Used by HyDE expansion. Throwaway hypothetical answers; favour fast.
    hyde_model: str = "openrouter:deepseek/deepseek-v4-flash"

    # --- LLM behaviour --------------------------------------------------
    # Whether to enable Anthropic prompt caching on instructions / system
    # prompts. Free latency + cost win on Anthropic; ignored by other providers.
    anthropic_cache_instructions: bool = True

    # --- Server ---------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 7077  # arbitrary high port; chosen because "loci" → mnemonic

    # --- Absorb / job queue ---------------------------------------------
    # How many traces/explicit signals trigger an automatic absorb suggestion.
    # We never auto-run absorb (it's expensive); we surface the proposal.
    absorb_signal_threshold: int = 15
    # Forgetting policy: nodes with access_count==0 over N days *and* low
    # confidence become dismissed candidates at absorb. PLAN §Cost model.
    forgetting_inactivity_days: int = 30
    forgetting_confidence_floor: float = 0.3

    # --- Computed paths -------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "loci.sqlite"

    @property
    def blob_dir(self) -> Path:
        # Content-addressed storage. PLAN §Storage: "Raw blobs on disk,
        # content-addressed". Layout: <blob_dir>/<sha256[:2]>/<sha256[2:]>.
        return self.data_dir / "blobs"

    @property
    def model_cache_dir(self) -> Path:
        return self.data_dir / "models"

    def ensure_dirs(self) -> None:
        """Create storage directories if missing. Safe to call repeatedly."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self.model_cache_dir.mkdir(parents=True, exist_ok=True)

    # --- Helpers --------------------------------------------------------
    def secret(self, name: str) -> str | None:
        """Return the plain-text value of a SecretStr field, or None."""
        val = getattr(self, name, None)
        if val is None:
            return None
        if isinstance(val, SecretStr):
            return val.get_secret_value()
        return val


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance.

    Cached because pydantic-settings reads env vars at construction time and we
    don't want that cost on every call. Tests should clear the cache via
    `get_settings.cache_clear()` after `monkeypatch.setenv`.
    """
    return Settings()
