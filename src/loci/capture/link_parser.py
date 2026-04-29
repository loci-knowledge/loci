"""Link extraction from resource text.

Parses three kinds of links from raw text, using lightweight regex — no
external markdown or Obsidian libraries at this layer:

  wikilinks     [[Target Page]]  (with optional [[Target|alias]] stripped)
  citation_keys @key or [@key] (BibTeX-style)
  urls          [text](https://...) and bare https://... links

The `parse_links` function is the sync entry point used by the background
parse_links job. `resolve_wikilinks` looks up matched resource IDs for each
wikilink target using fuzzy title matching.

Important: this module does NOT write to concept_edges. That write is done by
the background job after calling parse_links() + resolve_wikilinks().
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# [[Target Page]] or [[Target Page|alias]]
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")

# @key or [@key; @key2] — BibTeX citation keys
# Require at least 3 chars after '@' to filter short false positives
_CITE_RE = re.compile(r"@([a-zA-Z][a-zA-Z0-9_:-]{2,})")

# Common false positive suffixes/patterns to exclude from citation keys
_CITE_EXCLUDE_RE = re.compile(
    r"(\w+\.\w+)"              # looks like an email/domain fragment
)

# Markdown-style links: [text](url)
_MD_LINK_RE = re.compile(r"\[(?:[^\]]*)\]\((https?://[^)\s]+)\)")

# Bare URLs (not already inside a markdown link)
_BARE_URL_RE = re.compile(r"(?<!\()(https?://[^\s)>\]\"']+)")

# Email addresses to exclude from citation key matches
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


@dataclass
class ParsedLinks:
    wikilinks: list[str] = field(default_factory=list)
    citation_keys: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)


def parse_links(text: str, subkind: str) -> ParsedLinks:
    """Extract wikilinks, citation keys, and URLs from resource text.

    Uses regex only. The `subkind` parameter is kept for future
    subkind-specific handling (e.g. skip citation parsing for code files).
    """
    wikilinks = _extract_wikilinks(text)
    citation_keys = _extract_citation_keys(text, subkind)
    urls = _extract_urls(text)

    return ParsedLinks(
        wikilinks=wikilinks,
        citation_keys=citation_keys,
        urls=urls,
    )


def resolve_wikilinks(
    wikilinks: list[str],
    project_id: str,
    conn: sqlite3.Connection,
) -> list[tuple[str, str]]:
    """Resolve wikilink targets to resource IDs via title matching.

    Returns [(wikilink_target, resource_id)] for successfully matched links.
    Tries exact title match first; falls back to rapidfuzz if the exact match
    fails, with a cutoff of 80.
    """
    if not wikilinks:
        return []

    # Load all titles in the project to avoid N+1 queries.
    rows = conn.execute(
        """
        SELECT n.id, n.title
        FROM nodes n
        JOIN project_effective_members pm ON pm.node_id = n.id
        WHERE pm.project_id = ? AND n.kind = 'raw'
        """,
        (project_id,),
    ).fetchall()

    if not rows:
        return []

    # Build lookup structures
    title_to_id: dict[str, str] = {}
    all_titles: list[str] = []
    for row in rows:
        t = (row["title"] or "").strip()
        if t:
            title_to_id[t.lower()] = row["id"]
            all_titles.append(t)

    resolved: list[tuple[str, str]] = []
    unique_wikilinks = list(dict.fromkeys(wikilinks))  # deduplicate, preserve order

    try:
        from rapidfuzz import fuzz as rf_fuzz
        from rapidfuzz import process as rf_process
        _has_rapidfuzz = True
    except ImportError:
        log.warning("resolve_wikilinks: rapidfuzz not installed; exact match only")
        _has_rapidfuzz = False

    for target in unique_wikilinks:
        target_lower = target.strip().lower()

        # Exact match first
        if target_lower in title_to_id:
            resolved.append((target, title_to_id[target_lower]))
            continue

        # Fuzzy match
        if _has_rapidfuzz and all_titles:
            best = rf_process.extractOne(
                target,
                all_titles,
                scorer=rf_fuzz.token_set_ratio,
                score_cutoff=80,
            )
            if best is not None:
                matched_title, _score, _idx = best
                node_id = title_to_id.get(matched_title.lower())
                if node_id:
                    resolved.append((target, node_id))

    return resolved


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _extract_wikilinks(text: str) -> list[str]:
    """Find all [[Target]] references, returning unique targets."""
    seen: set[str] = set()
    result: list[str] = []
    for m in _WIKILINK_RE.finditer(text):
        target = m.group(1).strip()
        if target and target not in seen:
            seen.add(target)
            result.append(target)
    return result


def _extract_citation_keys(text: str, subkind: str) -> list[str]:
    """Find all @key citation references, filtering common false positives."""
    if subkind == "code":
        # Skip citation parsing for code — '@' is used for decorators etc.
        return []

    seen: set[str] = set()
    result: list[str] = []

    for m in _CITE_RE.finditer(text):
        key = m.group(1)
        if key in seen:
            continue

        # Filter: looks like an email address
        # Check the broader context: if the character before '@' is not whitespace/[
        char_before_pos = m.start() - 1
        if char_before_pos >= 0:
            char_before = text[char_before_pos]
            # Email pattern: word@word.tld — skip if there's a non-whitespace/bracket char
            if char_before not in (" ", "\t", "\n", "[", "(", ",", ";"):
                continue

        # Filter: common false positives (version strings, emails in .bib files)
        if _EMAIL_RE.match(key):
            continue

        seen.add(key)
        result.append(key)

    return result


def _extract_urls(text: str) -> list[str]:
    """Find all http(s) URLs (markdown-style and bare), deduplicated."""
    seen: set[str] = set()
    result: list[str] = []

    # Collect markdown-style links first
    for m in _MD_LINK_RE.finditer(text):
        url = m.group(1)
        if url not in seen:
            seen.add(url)
            result.append(url)

    # Then bare URLs, skipping positions already consumed by markdown links
    # (rapidfuzz is only needed in resolve_wikilinks, not here)
    for m in _BARE_URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;:!?")
        if url not in seen:
            seen.add(url)
            result.append(url)

    return result
