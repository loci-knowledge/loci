# Model configuration

loci uses [pydantic-ai](https://ai.pydantic.dev) under the hood, which means
you can mix Anthropic, OpenAI, and OpenRouter models freely. Different tasks
in loci can run on different providers — you might want a strong reasoning
model for interpretation maintenance and a cheap fast model for HyDE.

## The four LLM tasks

| Setting               | What uses it                                        | Default                  |
|-----------------------|-----------------------------------------------------|--------------------------|
| `interpretation_model`| Kickoff (project → first tensions) and the silent reflection cycle that creates / reinforces / softens interpretations after every draft | `openai:gpt-5.4-mini` |
| `rag_model`           | `loci draft` — synthesises markdown with citations  | `openai:gpt-5.4-nano`    |
| `classifier_model`    | Absorb's contradiction pass: 3-way classifier per (raw, interp) pair | `openai:gpt-5.4-nano` |
| `hyde_model`          | HyDE expansion in retrieval                         | `openai:gpt-5.4-nano`    |

The picks above optimize for: strong reasoning where it matters, fast/cheap
where the calls are high-frequency. Override any of them in `.env`:

```bash
LOCI_INTERPRETATION_MODEL=anthropic:claude-opus-4-7
LOCI_RAG_MODEL=anthropic:claude-sonnet-4-6
LOCI_CLASSIFIER_MODEL=openrouter:google/gemini-3-pro
LOCI_HYDE_MODEL=openai:gpt-5.4-nano
```

## Provider keys

Read from the standard env vars (no `LOCI_` prefix):

```bash
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
OPENROUTER_API_KEY=sk-or-...
```

Each is a [`SecretStr`](https://docs.pydantic.dev/latest/api/types/#pydantic.types.SecretStr)
in `Settings`, so the value never appears in logs.

If a configured task points at a provider whose key is missing, the task
**degrades gracefully**:

- `loci draft` returns a stub showing the candidates it would have used.
- HyDE silently falls back to the raw query.
- The contradiction pass logs `skipped: no_anthropic` and continues.
- The kickoff job returns `skipped: true`.

This means you can run a fully working `loci q` (lex + vec + PPR — no LLM
involved) with zero keys configured.

## Model spec format

Every model setting is a string of the form `<provider>:<model_name>`:

```
anthropic:claude-opus-4-7
anthropic:claude-sonnet-4-6
anthropic:claude-haiku-4-5-20251001

openai:gpt-5
openai:gpt-5-mini
openai:gpt-4o

openrouter:google/gemini-2.0-flash-exp
openrouter:anthropic/claude-sonnet-4-6
openrouter:meta-llama/llama-3.3-70b-instruct
openrouter:deepseek/deepseek-r1
```

Providers we accept today: `anthropic`, `openai`, `openrouter`.
Pydantic-ai supports more (Google, Cohere, Groq, Mistral, …); add them by
extending `loci/llm/agent.py:_build_model`.

## Provider-specific behaviour

### Anthropic — prompt caching enabled by default

When the resolved model is Anthropic, loci automatically attaches
`AnthropicModelSettings(anthropic_cache_instructions='1h')`. The system
prompt sits in cache for the full hour TTL — repeated `loci draft` calls in
the same writing session are cheap.

Toggle with `LOCI_ANTHROPIC_CACHE_INSTRUCTIONS=false` if you have a reason
not to cache.

### OpenRouter — uses the OpenAI-compat endpoint

Loci sends requests to OpenRouter via `OpenAIChatModel + OpenRouterProvider`
under the hood; OpenRouter forwards them to whichever upstream you specified
(after `openrouter:`). You pay per-token via OpenRouter; the upstream
provider doesn't see your API key.

If you want Anthropic prompt caching but route through OpenRouter, that
*should* work end-to-end (OpenRouter passes the cache_control headers
through), but loci doesn't currently set the cache header for non-Anthropic
provider classes. File an issue if this matters for your use case.

### OpenAI — no special behaviour

Standard chat completions API. Structured output (`output_type=...`) uses
pydantic-ai's `ToolOutput` strategy by default — most portable.

## Switching providers mid-project

Switching is just changing env vars + restarting the server (or re-running
the CLI). Nothing in the database is provider-specific. Existing nodes,
edges, embeddings, and traces stay valid.

You can also mix providers within a single absorb run — the contradiction
classifier might be on Gemini while drafts are on Claude. As long as the
output schema is honoured (pydantic-ai enforces this for structured output
calls), nothing else cares.

## What's not provider-agnostic

The **embedding model** is local-only and pinned to `BAAI/bge-small-en-v1.5`
(384-d) by the schema (the `node_vec` table is `FLOAT[384]`). Swapping
embedding model = a new SQL migration that creates a new vec table for the
new dimension, plus a re-embed job over every existing node. PLAN §Open
questions calls this out as a v1 limit.

## Quick smoke test

After setting at least one key:

```bash
uv run python -c "
import os, tempfile
os.environ['LOCI_DATA_DIR'] = tempfile.mkdtemp()
from loci.config import get_settings; get_settings.cache_clear()
from loci.llm import build_agent
agent = build_agent(get_settings().rag_model, instructions='Reply with the word OK.')
print(agent.run_sync('say it').output)
"
# → OK
```
