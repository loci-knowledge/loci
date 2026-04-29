"""LLM client wrappers.

We use **pydantic-ai** as the provider-neutral abstraction so users can mix
Anthropic, OpenAI, and OpenRouter for different tasks. Per-task models are
configured via `Settings.rag_model` and `Settings.hyde_model` (see
`loci/config.py`).

Public API:

    build_agent(spec, ...)     → pydantic_ai.Agent
"""

from loci.llm.agent import (
    LLMNotConfiguredError,
    build_agent,
)

__all__ = [
    "LLMNotConfiguredError",
    "build_agent",
]
