"""Text extractors per file type.

Each extractor reads a file and returns plain text suitable for embedding +
FTS. The dispatch table maps file suffix → extractor + subkind.

PDF dispatch order (highest-quality first; we use whatever's installed):

    1. marker (datalab-to/marker)   — OCR + tables + equations + multi-column.
                                       GPL-3 code, OpenRAIL-M weights, ~5s/page on MPS.
                                       Install: `loci[pdf-marker]`.
    2. pymupdf4llm                   — fast, markdown-out, no OCR. AGPL-3.
                                       Install: `loci[pdf-quality]`.
    3. pypdf                         — text-only, BSD, the safe default.

We sniff for the optional packages at import time so the cold path stays cheap.
The marker model dict is held in a module-level cache because reloading it
per-PDF re-downloads/loads several GB of weights.

Other formats:
    HTML — BeautifulSoup with lxml if available, html.parser otherwise.
    Code, markdown, text, transcripts — pass-through utf-8 read.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from loci.graph.models import RawSubkind

log = logging.getLogger(__name__)

# Capability flags — set at import time so we don't pay the import cost
# repeatedly. Imports themselves are lazy.
try:  # pragma: no cover - optional dep
    import pymupdf4llm  # type: ignore[import-not-found]  # noqa: F401
    _HAS_PYMUPDF4LLM = True
except ImportError:
    _HAS_PYMUPDF4LLM = False

try:  # pragma: no cover - optional dep
    import marker  # type: ignore[import-not-found]  # noqa: F401
    _HAS_MARKER = True
except ImportError:
    _HAS_MARKER = False


@dataclass
class ExtractedDoc:
    text: str
    mime: str
    subkind: RawSubkind


# Mapping suffix → (mime, subkind). Used for routing + storing on the RawNode.
SUFFIX_META: dict[str, tuple[str, RawSubkind]] = {
    ".md": ("text/markdown", "md"),
    ".mdx": ("text/markdown", "md"),
    ".markdown": ("text/markdown", "md"),
    ".txt": ("text/plain", "txt"),
    ".rst": ("text/x-rst", "txt"),
    ".org": ("text/x-org", "txt"),
    ".pdf": ("application/pdf", "pdf"),
    ".html": ("text/html", "html"),
    ".htm": ("text/html", "html"),
    ".vtt": ("text/vtt", "transcript"),
    ".srt": ("application/x-subrip", "transcript"),
    # Code (subkind=code, mime varies)
    ".py": ("text/x-python", "code"),
    ".js": ("application/javascript", "code"),
    ".ts": ("application/typescript", "code"),
    ".jsx": ("text/jsx", "code"),
    ".tsx": ("text/tsx", "code"),
    ".rs": ("text/x-rust", "code"),
    ".go": ("text/x-go", "code"),
    ".rb": ("text/x-ruby", "code"),
    ".java": ("text/x-java", "code"),
    ".c": ("text/x-c", "code"),
    ".cc": ("text/x-c++", "code"),
    ".cpp": ("text/x-c++", "code"),
    ".h": ("text/x-c", "code"),
    ".hpp": ("text/x-c++", "code"),
    ".cs": ("text/x-csharp", "code"),
    ".swift": ("text/x-swift", "code"),
    ".kt": ("text/x-kotlin", "code"),
    ".scala": ("text/x-scala", "code"),
    ".sh": ("application/x-sh", "code"),
    ".bash": ("application/x-sh", "code"),
    ".zsh": ("application/x-sh", "code"),
    ".sql": ("application/sql", "code"),
    ".lua": ("text/x-lua", "code"),
    ".r": ("text/x-r", "code"),
    ".R": ("text/x-r", "code"),
    ".jl": ("text/x-julia", "code"),
    ".toml": ("application/toml", "code"),
    ".yaml": ("application/yaml", "code"),
    ".yml": ("application/yaml", "code"),
    ".json": ("application/json", "code"),
}


def extract(path: Path) -> ExtractedDoc | None:
    """Extract a file. Returns None if the file is unsupported / unreadable."""
    suffix = path.suffix.lower()
    meta = SUFFIX_META.get(suffix)
    if meta is None:
        return None
    mime, subkind = meta

    if subkind == "pdf":
        text = _extract_pdf(path)
    elif subkind == "html":
        text = _extract_html(path)
    else:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("extract: %s read failed: %s", path, exc)
            return None
    if not text.strip():
        return None
    return ExtractedDoc(text=text, mime=mime, subkind=subkind)


# ---------------------------------------------------------------------------
# PDF dispatch
# ---------------------------------------------------------------------------


def _extract_pdf(path: Path) -> str:
    """marker → pymupdf4llm → pypdf, in order of preference."""
    if _HAS_MARKER:  # pragma: no cover - heavy optional path
        try:
            return _extract_pdf_marker(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("marker failed for %s: %s; falling back", path, exc)
    if _HAS_PYMUPDF4LLM:  # pragma: no cover - optional dep
        try:
            return _extract_pdf_pymupdf4llm(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("pymupdf4llm failed for %s: %s; falling back to pypdf", path, exc)
    return _extract_pdf_pypdf(path)


def _extract_pdf_pymupdf4llm(path: Path) -> str:  # pragma: no cover
    import pymupdf4llm  # type: ignore[import-not-found]
    return pymupdf4llm.to_markdown(str(path))


def _extract_pdf_pypdf(path: Path) -> str:
    from pypdf import PdfReader
    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001 — pypdf raises a zoo
        log.warning("pypdf failed to open %s: %s", path, exc)
        return ""
    pages: list[str] = []
    for page in reader.pages:
        try:
            txt = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            log.debug("page extract failed in %s: %s", path, exc)
            continue
        if txt.strip():
            pages.append(txt)
    return "\n\n".join(pages)


# ---------------------------------------------------------------------------
# marker integration
#
# marker downloads several GB of weights on first run and takes ~30 s to load
# them. We hold a single PdfConverter at module scope so subsequent calls reuse
# the warm models. The lock prevents two threads from racing the construction.
# ---------------------------------------------------------------------------

_marker_lock = threading.Lock()
_marker_converter = None  # type: ignore[var-annotated]


def _get_marker_converter():  # pragma: no cover - heavy optional path
    """Lazily build (and cache) a marker PdfConverter."""
    global _marker_converter
    if _marker_converter is not None:
        return _marker_converter
    with _marker_lock:
        if _marker_converter is not None:
            return _marker_converter
        log.info("marker: loading models (one-time, several GB)…")
        from marker.converters.pdf import PdfConverter  # type: ignore[import-not-found]
        from marker.models import create_model_dict  # type: ignore[import-not-found]
        _marker_converter = PdfConverter(artifact_dict=create_model_dict())
        return _marker_converter


def _extract_pdf_marker(path: Path) -> str:  # pragma: no cover - heavy optional path
    """High-quality marker extraction. Reuses a warm converter."""
    from marker.output import text_from_rendered  # type: ignore[import-not-found]
    converter = _get_marker_converter()
    rendered = converter(str(path))
    markdown, _meta, _images = text_from_rendered(rendered)
    return markdown


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def _extract_html(path: Path) -> str:
    from bs4 import BeautifulSoup
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("html read failed for %s: %s", path, exc)
        return ""
    try:
        soup = BeautifulSoup(raw, "lxml")
    except Exception:
        soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()
    return soup.get_text(separator="\n").strip()
