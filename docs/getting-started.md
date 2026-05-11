# Getting started

This walks through the full loop: install → create a project → register MCP →
save sources → get project-aware recall from Claude Code.

The core idea: you save once, and loci automatically tags, interprets, and
re-interprets each resource in the context of each project it belongs to — no
manual labelling required after the initial save.

## 1. Install

```bash
# recommended — isolated environment via uv
uv tool install loci-wiki

# or with pipx
pipx install loci-wiki
```

Verify:

```bash
loci --version
loci doctor
```

## 2. First-run setup

```bash
loci config init
```

Writes:

- `~/.loci/.env` — provider API keys (`chmod 600`). Add one of:
  `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, or `ANTHROPIC_API_KEY`.
- `~/.loci/config.toml` — non-secret settings (model IDs, port, feature flags).

## 3. Create a project

A *project* is a profile + a scoped view of your resources. Give it a name and
a short description of your goals — this description is the project *profile*
that guides all interpretation.

```bash
loci project create codoc
# Wizard asks: title, description, optional seed aspects
```

The profile matters: "Building a documentation sync tool for codebases" produces
different interpretations than "Studying programming language theory" — even for
the same paper.

## 4. Register with Claude Code

One time, user-scope (works from every directory):

```bash
claude mcp add loci --transport stdio --scope user -- loci mcp
claude mcp get loci   # verify
```

> No HTTP server needed for MCP. `loci mcp` is a stdio subprocess that Claude
> Code spawns directly.

## 5. Bind a directory

So MCP knows which project to use automatically:

```bash
cd ~/Documents/codoc
loci project bind codoc
```

Writes `.loci/project.toml`. Commit it if you want the binding in git.

Other ways (first match wins): `.mcp.json` env `LOCI_PROJECT=codoc`, session
pin `loci current set codoc`, or pass `project="codoc"` in each tool call.

## 6. Save sources

### From Claude Code (preferred)

```
Use loci_save to save https://arxiv.org/abs/2312.00001
```

loci:
1. Fetches the URL, extracts text, hashes, deduplicates, embeds.
2. Suggests a folder via fuzzy match against existing folders.
3. Suggests 3-7 aspects via KeyBERT.
4. Opens an elicitation form — pick a folder, review aspects, add a note. Hit save.
5. Queues background jobs immediately (no delay for first save).

### From the CLI

```bash
loci save https://arxiv.org/abs/2312.00001 --folder papers
loci save ~/Downloads/paper.pdf --folder papers
loci save "Key insight: documentation freshness is a latency problem..."
```

## 7. What happens in the background (automatic)

You don't need to do anything after saving. The worker runs continuously:

```
classify_aspects          (runs after each ingest)
    labels: "literate programming", "WEB system", "program documentation"
    → these are global tags, shared across all projects

infer_interpretation      (runs per project, daily-bucketed)
    codoc project output:
      stance: supporting
      summary: "Knuth's WEB system is a strong historical foundation for CoDoc:
        it validates the premise that documentation should be a primary
        programming artifact and that software should be organized for human
        understanding."
      aspects: "Historical foundation for doc-as-primary artifact",
               "Lacks bidirectional synchronization (CoDoc's key differentiator)",
               "Methodological precedent for integrating code and explanation"
```

Check progress:

```bash
loci status codoc
```

## 8. Recall with project-aware reasons

```
loci_recall("documentation generation approaches")
```

Returns ranked resources. Each result shows a *why surfaced* reason that reads
the resource through your project's lens:

```
### 1. Knuth — Literate Programming   `papers`
Aspects: literate programming, program documentation, WEB system
Why surfaced: In this project (supporting): validates the premise that
  documentation should be a primary programming artifact.
```

The same paper in a different project (`plm`) might return:

```
Why surfaced: In this project (methodological): foundational precedent for
  treating programs as communicative artifacts; tangling/weaving anticipates
  modern macro systems.
```

## 9. Edit aspects

User-set labels are *gold* — the LLM never overwrites them. When you add or
remove aspects, a `refresh_project_edges` job automatically updates the
co-aspect graph.

### From Claude Code

```
loci_aspects(resource_id="01KQDB5E...", action="add", labels=["freshness", "evaluation"])
loci_aspects(resource_id="01KQDB5E...", action="edit")   # opens elicitation form
```

### From the CLI

```bash
loci aspects 01KQDB5E --add freshness --remove "WEB system"
loci aspects --list-vocab      # browse the project's full vocabulary
```

### Cascades from edits

When you add/remove aspects:
1. The `resource_aspects` row is updated immediately (gold, source=`user`).
2. A `refresh_project_edges` job recomputes co-aspect edges between resources.
3. The next `infer_interpretation` run (daily) uses your gold labels as constraints.

## 10. @-mention resources directly

```
@loci:source://01KQDB5ECMH0YYVZS61X0KMHA9    full text of one resource
@loci:folder://papers                          list resources in "papers" folder
@loci:aspect://literate-programming            resources tagged with this aspect
```

> `@loci:` autocomplete shows URI templates, not enumerable resources. Use
> `loci_browse` first to find a resource ID, then paste into `@loci:source://`.

## 11. Workspaces — bulk scan

A *workspace* is a directory tree you scan in bulk. Best for "I have a folder
of 200 PDFs" cases.

```bash
loci workspace create literature
loci workspace add-source literature ~/Documents/papers
loci workspace link literature codoc
loci workspace scan literature
# → all PDFs are ingested, classified, and interpretation jobs are queued
```

## 12. Passive inference via conversation hooks (optional)

loci can pick up interpretation signals from your Claude Code session without
any explicit tool calls. Enable in `~/.loci/config.toml`:

```toml
capture_conversation = true
```

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [{"hooks": [{"type": "command",
      "command": "loci event conversation --role user"}]}],
    "Stop": [{"hooks": [{"type": "command",
      "command": "loci event conversation --role assistant"}]}]
  }
}
```

loci fuzzy-matches your prompts against the project's top aspects. If a match
crosses the relevance threshold, it queues `infer_interpretation` for the matched
resources using the daily bucket.

## 13. Day-to-day reference

```bash
loci status [project]        # resource count + queued jobs + top aspects
loci doctor                  # paths, migration version, embedder, API keys
loci export [project]        # snapshot to ~/.loci/exports/
loci reset                   # wipe everything (asks for confirmation)
```

## Troubleshooting

| symptom | fix |
|---|---|
| MCP returns `Error: project not found` | Run `loci project bind <slug>` in your working dir or set `LOCI_PROJECT` |
| First scan is slow | Embedding model downloads once (~130 MB) on first use |
| `loci_save` skips elicitation form | Client doesn't support elicitation — pass `folder=` and `aspects=` explicitly |
| Aspects are in wrong language | The LLM uses the document's language by default; edit via `loci_aspects` to override |
| `infer_interpretation` shows `skipped: inputs_unchanged` | Content, profile, and query pattern haven't changed since last run — this is correct behaviour |
| `loci doctor` flags missing API key | Edit `~/.loci/.env` and run `chmod 600 ~/.loci/.env` |

For the architecture deep-dive, see [`architecture.md`](./architecture.md).
