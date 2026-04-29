"""Runtime settings for loci.

All settings are sourced (in order of precedence — first wins) from:

1. Environment variables prefixed `LOCI_` (e.g. `LOCI_DATA_DIR=/tmp/loci`).
2. A `.env` file in the working directory.
3. A `.env` file at `~/.loci/.env` (per-install defaults).
4. A `config.toml` file at `~/.loci/config.toml` (declarative install config).
5. The defaults below.

The single source of truth is the `Settings` instance returned by `get_settings()`.
We expose it as a function (not a module-level constant) so tests can override
paths via `monkeypatch.setenv` and re-create the instance.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LOCI_",
        # Order matters: pydantic-settings loads files left-to-right and the
        # *last* loaded file wins. By placing `~/.loci/.env` first and `.env`
        # second, the cwd `.env` overrides the per-install one.
        env_file=[str(Path.home() / ".loci" / ".env"), ".env"],
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

    # --- LLM provider keys ----------------------------------------------
    # Each key is read from the standard provider env var (no LOCI_ prefix)
    # so users don't have to set two variables. SecretStr ensures the value
    # never appears in repr/log output.
    anthropic_api_key: SecretStr | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    openrouter_api_key: SecretStr | None = Field(default=None, alias="OPENROUTER_API_KEY")
    openrouter_api_key_backup: SecretStr | None = Field(default=None, alias="OPENROUTER_API_KEY_BACKUP")

    # --- LLM model selection (per-task) ---------------------------------
    # Each spec is `<provider>:<model_name>`. Providers: anthropic, openai,
    # openrouter. Tasks that don't need an LLM (lex, vec, ingest) ignore
    # these entirely.

    # Used for LLM-driven aspect classification (capture/classify_aspects job)
    # and any other pipeline that needs strong instruction following.
    rag_model: str = "openrouter:anthropic/claude-opus-4.7"

    # Used by HyDE expansion. Throwaway hypothetical answers; favour fast.
    hyde_model: str = "openrouter:deepseek/deepseek-v4-flash"

    # --- LLM behaviour --------------------------------------------------
    # Whether to enable Anthropic prompt caching on instructions / system
    # prompts. Free latency + cost win on Anthropic; ignored by other providers.
    anthropic_cache_instructions: bool = True

    # --- Server ---------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 7077  # arbitrary high port; chosen because "loci" → mnemonic

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

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    def ensure_dirs(self) -> None:
        """Create storage directories if missing. Safe to call repeatedly."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self.model_cache_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        from pydantic_settings import TomlConfigSettingsSource
        toml_path = Path.home() / ".loci" / "config.toml"
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls, toml_file=str(toml_path)),
        )

    # --- Helpers --------------------------------------------------------
    def secret(self, name: str) -> str | None:
        """Return the plain-text value of a SecretStr field, or None.

        For openrouter_api_key: falls back to openrouter_api_key_backup when
        the primary is absent or appears invalid (wrong prefix / too short).
        """
        val = getattr(self, name, None)
        result = val.get_secret_value() if isinstance(val, SecretStr) else val
        if name == "openrouter_api_key" and not _looks_valid(result):
            backup = getattr(self, "openrouter_api_key_backup", None)
            if backup is not None:
                candidate = backup.get_secret_value() if isinstance(backup, SecretStr) else backup
                if _looks_valid(candidate):
                    return candidate
        return result


def _looks_valid(key: str | None) -> bool:
    """Quick sanity check: non-empty and at least 20 chars."""
    return bool(key and len(key) >= 20)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance.

    Cached because pydantic-settings reads env vars at construction time and we
    don't want that cost on every call. Tests should clear the cache via
    `get_settings.cache_clear()` after `monkeypatch.setenv`.
    """
    return Settings()
