# Getting started

This walks through the full loop: install → create a project → register MCP →
save a source → recall it from Claude Code.

If you only want the headline, the loop is:

```
save → tag → recall
```

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
loci --help
```

## 2. First-run setup

```bash
loci config init
```

This writes:

- `~/.loci/.env` — provider API keys (`chmod 600`). Add at least one of
  `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, or `ANTHROPIC_API_KEY`.
- `~/.loci/config.toml` — non-secret settings (model IDs, embedding device,
  port).

Sanity-check paths and reachability:

```bash
loci doctor
```

## 3. Create a project

A *project* is a profile + membership view over your sources. Most users have
one project per long-running area of work (a thesis, a product, a course).

```bash
loci project create my-research
```

The wizard asks for a title, description, and optional aspect seed labels.
You can also use `loci project manage` later to edit any of it.

List projects:

```bash
loci project list
```

## 4. Register loci with Claude Code

One time, user-scope (works from every directory):

```bash
claude mcp add loci --transport stdio --scope user -- loci mcp
```

Verify:

```bash
claude mcp get loci
```

## 5. Tell loci which project a directory belongs to

So MCP tools know which slug to use, bind a directory to your project:

```bash
cd ~/Documents/my-research
loci project bind my-research
```

This writes `.loci/project.toml` in that directory. MCP tools walk up the tree
to find it. Commit it if you want the binding tracked in git.

Alternative options (first match wins):

- **Per-workspace `.mcp.json`** with `LOCI_PROJECT=<slug>` in `env`.
- **Session pin**: `loci current set my-research` — applies to every MCP
  session until you `loci current clear`.
- **Inline**: pass `project="my-research"` in any tool call.

## 6. Save your first source

From Claude Code (preferred — uses the MCP elicitation form for folder +
aspects):

```
Use loci_save to save https://arxiv.org/abs/1612.03975
```

loci will:

1. Fetch the URL, extract text, hash, dedup, embed.
2. Suggest a folder via fuzzy match against existing folders.
3. Suggest aspects via KeyBERT over the first chunks.
4. Show you a form: pick a folder, check the aspects you want, add a context
   note. Hit save.
5. Queue background jobs to refine aspects (LLM) and parse citations /
   wikilinks into the concept graph.

From the CLI:

```bash
loci save https://arxiv.org/abs/1612.03975 \
  --folder papers/kb-construction \
  --aspects methodology,knowledge-graph
```

Save a local file:

```bash
loci save ~/Downloads/paper.pdf
```

Save a snippet:

```bash
loci save "Personalized PageRank with restart probability ~ 0.15..."
```

## 7. Recall

From Claude Code:

```
Use loci_recall to find sources about how PPR works in graph retrieval.
```

The result includes the ranked sources, their aspects, and a *why surfaced*
line that names the aspects / edges that promoted each one.

Or `@`-mention a specific resource directly:

```
@loci:source://<resource_id>
@loci:folder://papers/kb-construction
@loci:aspect://methodology
```

CLI:

```bash
loci recall "how does PPR work in graph retrieval"
```

## 8. Edit aspects

Aspects drift. Two ways to fix them:

```
Use loci_aspects to edit aspects on resource <id>.
```

(Opens an MCP form with the current labels checked.)

CLI:

```bash
loci aspects <resource_id> --add new-label --remove wrong-label
loci aspects --list-vocab                 # see the full project vocabulary
```

## 9. Workspaces (optional)

A *workspace* is a directory tree you scan in bulk. Useful for the "I have a
folder of 200 PDFs" case.

```bash
loci workspace create literature
loci workspace add-source literature ~/Documents/papers
loci workspace link literature my-research
loci workspace scan literature
```

Sources discovered during the scan land in your project automatically.

## 10. Day-to-day commands

```bash
loci status [project]        # counts + top aspects
loci use ws1 ws2             # bind workspaces for this session (.loci/session.toml)
loci doctor                  # paths, migrations, embedder, API
loci export [project]        # write graph.json + memo.md snapshots
loci reset                   # nuke everything (asks for confirmation)
```

## Troubleshooting

| symptom | fix |
|---------|-----|
| MCP tool returns `Error: project not found` | bind a project (`loci project bind <slug>`) or set `LOCI_PROJECT` in your `.mcp.json` env |
| First scan is slow | the embedding model (`BAAI/bge-small-en-v1.5`, ~130 MB) downloads once into `~/.loci/models/` |
| `loci_save` skips elicitation | the client doesn't advertise elicitation support — pass `folder` and `aspects` explicitly |
| `loci doctor` flags a missing key | edit `~/.loci/.env` and `chmod 600 ~/.loci/.env` |

For deeper reading, see [`architecture.md`](./architecture.md).
