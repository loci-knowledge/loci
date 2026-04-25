"""LLM client wrappers.

We use **pydantic-ai** as the provider-neutral abstraction so users can mix
Anthropic, OpenAI, and OpenRouter for different tasks. Per-task models are
configured via `Settings.interpretation_model`, `rag_model`, `classifier_model`,
and `hyde_model` (see `loci/config.py`).

Public API:

    has_credentials_for(spec)  → bool
    build_agent(spec, ...)     → pydantic_ai.Agent
"""

from loci.llm.agent import (
    LLMNotConfiguredError,
    build_agent,
    has_credentials_for,
    parse_spec,
)

__all__ = [
    "LLMNotConfiguredError",
    "build_agent",
    "has_credentials_for",
    "parse_spec",
]
