"""Use-case modules: one per cross-cutting operation.

Each module owns shared orchestration (project validation, core operation,
citation tracking, reflect enqueue). Surfaces (MCP, HTTP, CLI) call in as
thin adapters and handle only presentation and broadcasting.
"""
