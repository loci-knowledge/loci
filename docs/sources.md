# Sources — where files live, what loci can read

## Registering roots

A "source" is a directory or file you've registered with a **workspace**.
Files can live anywhere on your filesystem; loci just walks the roots you
tell it about. Workspaces decouple file registration from projects: scan
once, link to many.

```bash
loci workspace add-source <ws-slug> <path> --label <label>   # register a root
loci workspace list-sources <ws-slug>                         # show all roots
loci workspace remove-source <ws-slug> <path-or-id>          # un-register
loci workspace scan <ws-slug>                                 # walk every root
loci workspace scan <ws-slug> /one/off/path                   # ad-hoc root (also fine)
```

Same operations over REST:
- `POST /workspaces` / `GET /workspaces` — create and list workspaces
- `POST /workspaces/:id/sources` / `GET /workspaces/:id/sources` / `DELETE /workspaces/:id/sources/:sid` — manage roots
- `POST /workspaces/:id/scan` — trigger a scan
- `POST /projects/:pid/workspaces/:wid` / `DELETE /projects/:pid/workspaces/:wid` — link or unlink a workspace to a project

Multi-project sharing is automatic: link the same workspace to multiple
projects and all of them see the same raw nodes via the
`project_effective_members` view — no re-scanning, no duplication. If the
same PDF is also registered under a second workspace that another project
uses, loci still stores **one RawNode** (deduped by content hash) and
two workspace memberships. The same paper in two projects participates in
both retrieval contexts but stays one row.

## Supported file types (built-in)

| Suffix(es)                        | Subkind        | Extractor              |
|-----------------------------------|----------------|------------------------|
| `.md`, `.mdx`, `.markdown`        | `md`           | utf-8 read             |
| `.txt`, `.rst`, `.org`            | `txt`          | utf-8 read             |
| `.pdf`                            | `pdf`          | marker → pymupdf4llm → pypdf (whichever is installed; see below) |
| `.html`, `.htm`                   | `html`         | BeautifulSoup (lxml if available) |
| `.vtt`, `.srt`                    | `transcript`   | utf-8 read             |
| `.py` `.js` `.ts` `.jsx` `.tsx` `.rs` `.go` `.rb` `.java` `.c` `.cc` `.cpp` `.h` `.hpp` `.cs` `.swift` `.kt` `.scala` `.sh` `.sql` `.lua` `.r` `.jl` `.toml` `.yaml` `.json` | `code` | utf-8 read |

Files larger than 50 MB and files with extensions not in the table are
silently skipped (the walker logs them at debug level).

## PDF extraction

Three extractors, in order of preference (loci picks whichever is installed):

### 1. marker — best quality, heaviest

[datalab-to/marker](https://github.com/datalab-to/marker) does layout-aware,
table-aware, equation-aware, OCR-when-needed PDF→markdown. **Roughly 5 s/page
on Apple Silicon (MPS), ~3.5 GB RAM, ~3-5 GB of model weights downloaded on
first run.**

#### License caveat

- marker's **code** is GPL-3.0.
- marker's **weights** are OpenRAIL-M (free for personal/research/commercial
  under $2M revenue; paid license above that).

Using marker shapes the licensing of any product you build on top. For a
personal/local installation that's fine. Read marker's LICENSE before bundling.

#### Why it's not in `loci[pdf-marker]`

`marker-pdf` 1.10.x hard-pins `anthropic>=0.46.0,<0.47.0`, which conflicts
with `pydantic-ai-slim[anthropic]>=1.87.0`'s requirement of `anthropic>=0.96.0`.
We can't bundle both as a normal pip extras tag.

#### Two ways to use marker anyway

**A. Separate environment** — keep loci's env clean and run marker in its
own venv, then expose its CLI:
```bash
# in a different venv
pip install marker-pdf
marker_single ~/papers/foo.pdf  # produces foo.md alongside
```
Then `loci workspace add-source` the directory containing the converted .md
files. This is the cleanest model and avoids any dep conflict; you treat
marker as a one-shot conversion tool, not part of loci's runtime.

**B. Override marker's anthropic pin in loci's env** (advanced, fragile):
```bash
uv pip install marker-pdf --no-deps
# install marker's actual deps manually, EXCEPT anthropic
# (look at marker-pdf's pyproject for the list)
```
With this, loci's `extractors.py` will sniff `import marker` succeeds and
use it. Be prepared for marker calls to break if its newer anthropic
internals expect API shapes that differ from the version pydantic-ai is
pinning.

### 2. pymupdf4llm — fast, good quality, AGPL

```bash
uv sync --extra pdf-quality
```

Markdown output, table preservation, ~100× faster than marker on born-digital
PDFs. Skips OCR — scanned PDFs come back empty. AGPL-3 (PyMuPDF) — opt-in.

### 3. pypdf — text-only, BSD, the always-available fallback

Already in loci's runtime deps. Loses tables, equations, and multi-column
layouts but works everywhere with no model downloads.

## Adding new file types

`loci/ingest/extractors.py` defines `SUFFIX_META` — a mapping of file suffix
to `(mime, subkind)`. To support a new format:

1. Add the suffix → `(mime, subkind)` entry. Subkind must be one of the
   `RawSubkind` literals: `pdf | md | code | html | transcript | txt | image`.
2. If extraction needs more than `path.read_text(...)`, write a small
   extractor function and dispatch to it from `extract()`.
3. Add the suffix to `loci/ingest/walker.py:DEFAULT_INCLUDE_EXTS` so the
   walker emits it.
4. Add a test in `tests/test_ingest.py`.

For binary formats (DOCX, EPUB, etc.) consider whether marker-pdf with the
`[full]` extras would handle it (DOCX/PPTX/XLSX/EPUB) — same caveats as the
PDF extractor.

## Files outside the include list

The walker silently skips:

- Anything in `.git`, `node_modules`, `.venv`, `__pycache__`, `dist`,
  `build`, `.next`, `.turbo`, `.cache`, `target`, etc.
- Any file whose name starts with `.` (dotfiles).
- Any file >50 MB (override `walker._accept` if you really need to).
- Any extension not in `DEFAULT_INCLUDE_EXTS`.

If you have a corpus that's mostly an unsupported format, convert it to MD
first (with whatever tool fits) and point loci at the converted directory.
That's also the cleanest path for marker users.

## Removing a file

Loci will not auto-detect a deleted file mid-session. The next absorb run
flips `source_of_truth` to `false` for any raw whose `canonical_path` is
missing, and surfaces `broken-support` proposals for interpretations that
cited it (PLAN §Edge cases (1)).

To force the audit immediately:

```bash
loci absorb <project>
```

To remove a source root from a workspace before the next scan (so new files
under it are no longer picked up):

```bash
loci workspace remove-source <ws-slug> <path-or-id>
```

Removing a source root does not immediately delete any already-scanned
RawNodes — those persist until the absorb audit marks them as missing.
