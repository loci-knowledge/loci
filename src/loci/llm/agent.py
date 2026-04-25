"""Per-task LLM agent factory built on pydantic-ai.

A model **spec** is a string of the form `<provider>:<model_name>`:

    anthropic:claude-opus-4-7
    openai:gpt-5.4
    openrouter:google/gemini-3-flash-preview
    openrouter:anthropic/claude-sonnet-4-6

`build_agent(spec, ...)` resolves the spec to a `pydantic_ai.Agent`. Callers
construct one Agent per task (per pydantic-ai's idiom — Agents are cheap and
type-parameterised by their `output_type`).

Anthropic prompt caching is wired up automatically: when the resolved model
is Anthropic and `Settings.anthropic_cache_instructions=True`, we attach
`AnthropicModelSettings(anthropic_cache_instructions='1h')` so the system
prompt sits in cache for the full hour TTL. Other providers ignore the flag.

If the model spec asks for a provider whose API key isn't configured, we raise
`LLMNotConfiguredError` — callers catch it and degrade gracefully (HyDE
returns the original query, the contradiction pass skips, draft returns a
stub). The retrieve / draft / kickoff routes never rely on a working LLM.

`pydantic-settings` (in `loci/config.py`) already reads `.env` at startup, so
we don't need an explicit `dotenv` load here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider

from loci.config import Settings, get_settings

log = logging.getLogger(__name__)


class LLMNotConfiguredError(RuntimeError):
    """Raised when a model spec requires an API key we don't have."""


@dataclass(frozen=True)
class _ParsedSpec:
    provider: str  # anthropic | openai | openrouter
    name: str      # provider-specific model name


def parse_spec(spec: str) -> _ParsedSpec:
    """Parse `provider:model_name`. Raises ValueError on malformed input."""
    if ":" not in spec:
        raise ValueError(
            f"model spec must be 'provider:name', got {spec!r}. "
            "Examples: 'anthropic:claude-sonnet-4-6', 'openai:gpt-5.4', "
            "'openrouter:google/gemini-3-flash-preview'."
        )
    provider, _, name = spec.partition(":")
    provider = provider.strip().lower()
    name = name.strip()
    if provider not in {"anthropic", "openai", "openrouter"}:
        raise ValueError(f"unknown provider {provider!r}; expected one of anthropic, openai, openrouter")
    if not name:
        raise ValueError(f"model spec missing model name: {spec!r}")
    return _ParsedSpec(provider=provider, name=name)


def has_credentials_for(spec: str, settings: Settings | None = None) -> bool:
    """Return True if the configured API key for `spec`'s provider is present."""
    settings = settings or get_settings()
    try:
        parsed = parse_spec(spec)
    except ValueError:
        return False
    key_field = {
        "anthropic": "anthropic_api_key",
        "openai": "openai_api_key",
        "openrouter": "openrouter_api_key",
    }[parsed.provider]
    return settings.secret(key_field) is not None


def build_agent(
    spec: str,
    *,
    instructions: str | None = None,
    output_type: type | Any = str,
    settings: Settings | None = None,
    enable_cache: bool | None = None,
) -> Agent:
    """Construct a pydantic-ai Agent for the given model spec.

    Parameters:
        spec:          provider:model spec string (see module docstring).
        instructions:  the system / instructions block. Anthropic-cached
                       automatically when applicable.
        output_type:   pass a Pydantic model for structured output, or `str`
                       for free-text. Pydantic-ai will enforce the schema.
        settings:      override the process-wide Settings; mostly for tests.
        enable_cache:  override `Settings.anthropic_cache_instructions`. None
                       defers to the setting.

    Raises LLMNotConfiguredError if the provider's API key is missing.
    """
    settings = settings or get_settings()
    parsed = parse_spec(spec)
    model = _build_model(parsed, settings)
    model_settings = _build_model_settings(parsed, settings, enable_cache)
    return Agent(
        model,
        instructions=instructions,
        output_type=output_type,
        model_settings=model_settings,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _build_model(parsed: _ParsedSpec, settings: Settings) -> AnthropicModel | OpenAIChatModel:
    """Return a concrete pydantic-ai model bound to a provider."""
    if parsed.provider == "anthropic":
        key = settings.secret("anthropic_api_key")
        if not key:
            raise LLMNotConfiguredError(
                "ANTHROPIC_API_KEY is not set; required for "
                f"model {parsed.name!r}."
            )
        return AnthropicModel(parsed.name, provider=AnthropicProvider(api_key=key))

    if parsed.provider == "openai":
        key = settings.secret("openai_api_key")
        if not key:
            raise LLMNotConfiguredError(
                "OPENAI_API_KEY is not set; required for "
                f"model {parsed.name!r}."
            )
        return OpenAIChatModel(parsed.name, provider=OpenAIProvider(api_key=key))

    if parsed.provider == "openrouter":
        # Try the primary key first; fall back to OPENROUTER_API_KEY_BACKUP
        # if the primary is absent or known to be invalid.
        key = _resolve_openrouter_key(settings)
        if not key:
            raise LLMNotConfiguredError(
                "OPENROUTER_API_KEY (or OPENROUTER_API_KEY_BACKUP) is not set; "
                f"required for model {parsed.name!r}."
            )
        # OpenRouter speaks the OpenAI chat completions protocol, so we wrap
        # OpenAIChatModel with an OpenRouterProvider that injects the right
        # base URL + headers (HTTP-Referer, X-Title) under the hood.
        return OpenAIChatModel(parsed.name, provider=OpenRouterProvider(api_key=key))

    raise AssertionError(f"unhandled provider {parsed.provider}")  # parse_spec guards this


@lru_cache(maxsize=1)
def _probe_openrouter_key(primary: str | None, backup: str | None) -> str | None:
    """Return the first valid OpenRouter key. Checked once per process (lru_cache)."""
    import httpx
    for key in (primary, backup):
        if not key:
            continue
        try:
            resp = httpx.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {key}"},
                timeout=5,
            )
            if resp.status_code == 200:
                return key
        except Exception:  # noqa: BLE001
            pass
    return primary or backup  # last resort: return whatever we have


def _resolve_openrouter_key(settings: Settings) -> str | None:
    """Return a working OpenRouter API key (probed once per process)."""
    primary = settings.secret("openrouter_api_key")
    backup = settings.secret("openrouter_api_key_backup")
    return _probe_openrouter_key(primary, backup)


def _build_model_settings(
    parsed: _ParsedSpec, settings: Settings, enable_cache: bool | None,
):
    """Provider-specific model_settings — currently just Anthropic prompt cache."""
    cache_on = settings.anthropic_cache_instructions if enable_cache is None else enable_cache
    if parsed.provider == "anthropic" and cache_on:
        # 1h cache TTL is the larger of the two breakpoints Anthropic exposes
        # and is plenty for an interactive writing session. The cost of the
        # cache write is amortised over every subsequent turn within the hour.
        return AnthropicModelSettings(anthropic_cache_instructions="1h")
    return None
