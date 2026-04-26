"""Paper discovery: HuggingFace Papers + Semantic Scholar + arXiv.

A standalone, pydantic-ai-friendly port of ml-intern's `papers_tool.py`. Each
public function is async, takes plain typed arguments, and returns a markdown
string — the agent reads the string directly. No ToolResult wrapper.

Operations:
  - trending(date?, query?)               daily HF trending
  - search(query, ...)                    HF search; uses Semantic Scholar
                                          when filters are present
  - paper_details(arxiv_id)               HF metadata + S2 if available
  - read_paper(arxiv_id, section?)        full ArXiv HTML reader
  - citation_graph(arxiv_id, direction?)  S2 references + citations
  - snippet_search(query, ...)            S2 full-text snippet search
  - recommend(arxiv_id?, positive_ids?)   S2 paper recommendations
  - find_datasets(arxiv_id, ...)          HF datasets linked to paper
  - find_models(arxiv_id, ...)            HF models linked to paper
  - find_collections(arxiv_id)            HF collections containing paper
  - find_all_resources(arxiv_id)          parallel datasets+models+collections

Sources:
  HF Hub:           https://huggingface.co/api
  Semantic Scholar: https://api.semanticscholar.org
  ArXiv HTML:       https://arxiv.org/html, https://ar5iv.labs.arxiv.org/html

Set `S2_API_KEY` in the environment for higher Semantic Scholar rate limits.
HF endpoints work without auth for read-only access.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

import httpx
from bs4 import BeautifulSoup, Tag

HF_API = "https://huggingface.co/api"
ARXIV_HTML = "https://arxiv.org/html"
AR5IV_HTML = "https://ar5iv.labs.arxiv.org/html"

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
MAX_SUMMARY_LEN = 300
MAX_SECTION_PREVIEW_LEN = 280
MAX_SECTION_TEXT_LEN = 8000

SORT_MAP = {
    "downloads": "downloads",
    "likes": "likes",
    "trending": "trendingScore",
}

# ---------------------------------------------------------------------------
# Semantic Scholar
# ---------------------------------------------------------------------------

S2_API = "https://api.semanticscholar.org"
S2_TIMEOUT = 12
_s2_last_request: float = 0.0
_s2_cache: dict[str, Any] = {}
_S2_CACHE_MAX = 500


def _s2_headers() -> dict[str, str]:
    key = os.environ.get("S2_API_KEY")
    return {"x-api-key": key} if key else {}


def _s2_paper_id(arxiv_id: str) -> str:
    return f"ARXIV:{arxiv_id}"


def _s2_cache_key(path: str, params: dict | None) -> str:
    p = tuple(sorted((params or {}).items()))
    return f"{path}:{p}"


async def _s2_request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    **kwargs: Any,
) -> httpx.Response | None:
    global _s2_last_request
    url = f"{S2_API}{path}"
    kwargs.setdefault("headers", {}).update(_s2_headers())
    kwargs.setdefault("timeout", S2_TIMEOUT)
    has_key = bool(os.environ.get("S2_API_KEY"))

    for attempt in range(3):
        if has_key:
            min_interval = 1.0 if "search" in path else 0.1
            elapsed = time.monotonic() - _s2_last_request
            if elapsed < min_interval:
                await asyncio.sleep(min_interval - elapsed)
        _s2_last_request = time.monotonic()

        try:
            resp = await client.request(method, url, **kwargs)
            if resp.status_code == 429:
                if attempt < 2:
                    await asyncio.sleep(60)
                    continue
                return None
            if resp.status_code >= 500:
                if attempt < 2:
                    await asyncio.sleep(3)
                    continue
                return None
            return resp
        except (httpx.RequestError, httpx.HTTPStatusError):
            if attempt < 2:
                await asyncio.sleep(3)
                continue
            return None
    return None


async def _s2_get_json(
    client: httpx.AsyncClient, path: str, params: dict | None = None,
) -> dict | None:
    key = _s2_cache_key(path, params)
    if key in _s2_cache:
        return _s2_cache[key]
    resp = await _s2_request(client, "GET", path, params=params or {})
    if resp and resp.status_code == 200:
        data = resp.json()
        if len(_s2_cache) < _S2_CACHE_MAX:
            _s2_cache[key] = data
        return data
    return None


# ---------------------------------------------------------------------------
# ArXiv HTML parsing
# ---------------------------------------------------------------------------


def _parse_paper_html(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.find("h1", class_="ltx_title")
    title = title_el.get_text(strip=True).removeprefix("Title:") if title_el else ""

    abstract_el = soup.find("div", class_="ltx_abstract")
    abstract = ""
    if abstract_el:
        for child in abstract_el.children:
            if isinstance(child, Tag) and child.name in ("h6", "h2", "h3", "p", "span"):
                if child.get_text(strip=True).lower() == "abstract":
                    continue
            if isinstance(child, Tag) and child.name == "p":
                abstract += child.get_text(separator=" ", strip=True) + " "
        abstract = abstract.strip()

    sections: list[dict[str, Any]] = []
    headings = soup.find_all(["h2", "h3"], class_=lambda c: c and "ltx_title" in c)

    for heading in headings:
        level = 2 if heading.name == "h2" else 3
        heading_text = heading.get_text(separator=" ", strip=True)

        text_parts: list[str] = []
        sibling = heading.find_next_sibling()
        while sibling:
            if isinstance(sibling, Tag):
                if sibling.name in ("h2", "h3") and "ltx_title" in (
                    sibling.get("class") or []
                ):
                    break
                if sibling.name == "h2" and level == 3:
                    break
                text_parts.append(sibling.get_text(separator=" ", strip=True))
            sibling = sibling.find_next_sibling()

        parent_section = heading.find_parent("section")
        if parent_section and not text_parts:
            for p in parent_section.find_all("p", recursive=False):
                text_parts.append(p.get_text(separator=" ", strip=True))

        section_text = "\n\n".join(t for t in text_parts if t)

        num_match = re.match(r"^([A-Z]?\d+(?:\.\d+)*)\s", heading_text)
        section_id = num_match.group(1) if num_match else ""

        sections.append({
            "id": section_id,
            "title": heading_text,
            "level": level,
            "text": section_text,
        })

    return {"title": title, "abstract": abstract, "sections": sections}


def _find_section(sections: list[dict], query: str) -> dict | None:
    q = query.lower().strip()
    for s in sections:
        if s["id"] == q or s["id"] == query:
            return s
    for s in sections:
        if q == s["title"].lower():
            return s
    for s in sections:
        if q in s["title"].lower():
            return s
    for s in sections:
        if s["id"].startswith(q + ".") or s["id"] == q:
            return s
    return None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _clean_description(text: str) -> str:
    text = re.sub(r"[\t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _format_paper_list(
    papers: list, title: str, date: str | None = None, query: str | None = None,
) -> str:
    lines = [f"# {title}"]
    if date:
        lines[0] += f" ({date})"
    if query:
        lines.append(f"Filtered by: '{query}'")
    lines.append(f"Showing {len(papers)} paper(s)\n")

    for i, item in enumerate(papers, 1):
        paper = item.get("paper", item)
        arxiv_id = paper.get("id", "")
        ptitle = paper.get("title", "Unknown")
        upvotes = paper.get("upvotes", 0)
        summary = paper.get("ai_summary") or _truncate(
            paper.get("summary", ""), MAX_SUMMARY_LEN,
        )
        keywords = paper.get("ai_keywords") or []
        github = paper.get("githubRepo") or ""
        stars = paper.get("githubStars") or 0

        lines.append(f"## {i}. {ptitle}")
        lines.append(f"**arxiv_id:** {arxiv_id} | **upvotes:** {upvotes}")
        lines.append(f"https://huggingface.co/papers/{arxiv_id}")
        if keywords:
            lines.append(f"**Keywords:** {', '.join(keywords[:5])}")
        if github:
            lines.append(f"**GitHub:** {github} ({stars} stars)")
        if summary:
            lines.append(f"**Summary:** {_truncate(summary, MAX_SUMMARY_LEN)}")
        lines.append("")
    return "\n".join(lines)


def _format_paper_detail(paper: dict, s2_data: dict | None = None) -> str:
    arxiv_id = paper.get("id", "")
    title = paper.get("title", "Unknown")
    upvotes = paper.get("upvotes", 0)
    ai_summary = paper.get("ai_summary") or ""
    summary = paper.get("summary", "")
    keywords = paper.get("ai_keywords") or []
    github = paper.get("githubRepo") or ""
    stars = paper.get("githubStars") or 0
    authors = paper.get("authors") or []

    lines = [f"# {title}"]
    meta_parts = [f"**arxiv_id:** {arxiv_id}", f"**upvotes:** {upvotes}"]
    if s2_data:
        cites = s2_data.get("citationCount", 0)
        influential = s2_data.get("influentialCitationCount", 0)
        meta_parts.append(f"**citations:** {cites} ({influential} influential)")
    lines.append(" | ".join(meta_parts))
    lines.append(f"https://huggingface.co/papers/{arxiv_id}")
    lines.append(f"https://arxiv.org/abs/{arxiv_id}")

    if authors:
        names = [a.get("name", "") for a in authors[:10]]
        author_str = ", ".join(n for n in names if n)
        if len(authors) > 10:
            author_str += f" (+{len(authors) - 10} more)"
        lines.append(f"**Authors:** {author_str}")

    if keywords:
        lines.append(f"**Keywords:** {', '.join(keywords)}")
    if s2_data and s2_data.get("s2FieldsOfStudy"):
        fields = [f["category"] for f in s2_data["s2FieldsOfStudy"] if f.get("category")]
        if fields:
            lines.append(f"**Fields:** {', '.join(fields)}")
    if s2_data and s2_data.get("venue"):
        lines.append(f"**Venue:** {s2_data['venue']}")
    if github:
        lines.append(f"**GitHub:** {github} ({stars} stars)")

    if s2_data and s2_data.get("tldr"):
        tldr_text = s2_data["tldr"].get("text", "")
        if tldr_text:
            lines.append(f"\n## TL;DR\n{tldr_text}")
    if ai_summary:
        lines.append(f"\n## AI Summary\n{ai_summary}")
    if summary:
        lines.append(f"\n## Abstract\n{_truncate(summary, 500)}")
    return "\n".join(lines)


def _format_read_paper_toc(parsed: dict[str, Any], arxiv_id: str) -> str:
    lines = [f"# {parsed['title']}"]
    lines.append(f"https://arxiv.org/abs/{arxiv_id}\n")
    if parsed["abstract"]:
        lines.append(f"## Abstract\n{parsed['abstract']}\n")
    lines.append("## Sections")
    for s in parsed["sections"]:
        prefix = "  " if s["level"] == 3 else ""
        preview = _truncate(s["text"], MAX_SECTION_PREVIEW_LEN) if s["text"] else "(empty)"
        lines.append(f"{prefix}- **{s['title']}**: {preview}")
    lines.append("\nCall read_paper(arxiv_id=..., section='4') for a specific section.")
    return "\n".join(lines)


def _format_read_paper_section(section: dict, arxiv_id: str) -> str:
    lines = [f"# {section['title']}"]
    lines.append(f"https://arxiv.org/abs/{arxiv_id}\n")
    text = section["text"]
    if len(text) > MAX_SECTION_TEXT_LEN:
        text = text[:MAX_SECTION_TEXT_LEN] + f"\n\n... (truncated at {MAX_SECTION_TEXT_LEN} chars)"
    lines.append(text or "(This section has no extractable text content.)")
    return "\n".join(lines)


def _format_s2_paper_list(papers: list[dict], title: str) -> str:
    lines = [f"# {title}"]
    lines.append(f"Showing {len(papers)} result(s)\n")
    for i, paper in enumerate(papers, 1):
        ptitle = paper.get("title") or "(untitled)"
        year = paper.get("year") or "?"
        cites = paper.get("citationCount", 0)
        venue = paper.get("venue") or ""
        ext_ids = paper.get("externalIds") or {}
        aid = ext_ids.get("ArXiv", "")
        tldr = (paper.get("tldr") or {}).get("text", "")
        lines.append(f"### {i}. {ptitle}")
        meta = [f"Year: {year}", f"Citations: {cites}"]
        if venue:
            meta.append(f"Venue: {venue}")
        if aid:
            meta.append(f"arxiv_id: {aid}")
        lines.append(" | ".join(meta))
        if aid:
            lines.append(f"https://arxiv.org/abs/{aid}")
        if tldr:
            lines.append(f"**TL;DR:** {tldr}")
        lines.append("")
    return "\n".join(lines)


def _format_citation_entry(entry: dict, show_context: bool = False) -> str:
    paper = entry.get("citingPaper") or entry.get("citedPaper") or {}
    title = paper.get("title") or "(untitled)"
    year = paper.get("year") or "?"
    cites = paper.get("citationCount", 0)
    ext_ids = paper.get("externalIds") or {}
    aid = ext_ids.get("ArXiv", "")
    influential = " **[influential]**" if entry.get("isInfluential") else ""
    parts = [f"- **{title}** ({year}, {cites} cites){influential}"]
    if aid:
        parts[0] += f"  arxiv:{aid}"
    if show_context:
        intents = entry.get("intents") or []
        if intents:
            parts.append(f"  Intent: {', '.join(intents)}")
        contexts = entry.get("contexts") or []
        for ctx in contexts[:2]:
            if ctx:
                parts.append(f"  > {_truncate(ctx, 200)}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public operations — each returns a markdown string
# ---------------------------------------------------------------------------


async def trending(
    date: str | None = None, query: str | None = None, limit: int = DEFAULT_LIMIT,
) -> str:
    """Daily HF trending papers, optionally filtered by keyword."""
    limit = min(limit, MAX_LIMIT)
    params: dict[str, Any] = {"limit": limit if not query else max(limit * 3, 30)}
    if date:
        params["date"] = date

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{HF_API}/daily_papers", params=params)
        resp.raise_for_status()
        papers = resp.json()

    if query:
        q = query.lower()
        papers = [
            p for p in papers
            if q in p.get("title", "").lower()
            or q in p.get("paper", {}).get("title", "").lower()
            or q in p.get("paper", {}).get("summary", "").lower()
            or any(q in kw.lower() for kw in (p.get("paper", {}).get("ai_keywords") or []))
        ]
    papers = papers[:limit]
    if not papers:
        msg = "No trending papers found"
        if query:
            msg += f" matching '{query}'"
        if date:
            msg += f" for {date}"
        return msg
    return _format_paper_list(papers, "Trending Papers", date=date, query=query)


async def _s2_bulk_search(
    query: str,
    limit: int,
    *,
    date_from: str = "",
    date_to: str = "",
    categories: str | None = None,
    min_citations: int | None = None,
    sort_by: str | None = None,
) -> str | None:
    params: dict[str, Any] = {
        "query": query,
        "limit": limit,
        "fields": "title,externalIds,year,citationCount,tldr,venue,publicationDate",
    }
    if date_from or date_to:
        params["publicationDateOrYear"] = f"{date_from}:{date_to}"
    if categories:
        params["fieldsOfStudy"] = categories
    if min_citations:
        params["minCitationCount"] = str(min_citations)
    if sort_by and sort_by != "relevance":
        params["sort"] = f"{sort_by}:desc"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await _s2_request(client, "GET", "/graph/v1/paper/search/bulk", params=params)
        if not resp or resp.status_code != 200:
            return None
        data = resp.json()

    papers = data.get("data") or []
    if not papers:
        return f"No papers found for '{query}' with the given filters."
    return _format_s2_paper_list(papers[:limit], f"Papers matching '{query}' (Semantic Scholar)")


async def search(
    query: str,
    limit: int = DEFAULT_LIMIT,
    *,
    date_from: str = "",
    date_to: str = "",
    categories: str | None = None,
    min_citations: int | None = None,
    sort_by: str | None = None,
) -> str:
    """Search papers. Filters route to Semantic Scholar; otherwise HF Papers."""
    limit = min(limit, MAX_LIMIT)
    use_s2 = bool(date_from or date_to or categories or min_citations or sort_by)
    if use_s2:
        result = await _s2_bulk_search(
            query, limit, date_from=date_from, date_to=date_to,
            categories=categories, min_citations=min_citations, sort_by=sort_by,
        )
        if result is not None:
            return result

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{HF_API}/papers/search", params={"q": query, "limit": limit},
        )
        resp.raise_for_status()
        papers = resp.json()
    if not papers:
        return f"No papers found for '{query}'"
    return _format_paper_list(papers, f"Papers matching '{query}'")


async def paper_details(arxiv_id: str) -> str:
    """Full paper metadata: HF + Semantic Scholar."""
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(f"{HF_API}/papers/{arxiv_id}")
            resp.raise_for_status()
            paper = resp.json()
        except httpx.HTTPError as exc:
            return f"Could not fetch paper {arxiv_id}: {exc}"
        s2_data = await _s2_get_json(
            client, f"/graph/v1/paper/{_s2_paper_id(arxiv_id)}",
            {"fields": "title,citationCount,influentialCitationCount,tldr,venue,s2FieldsOfStudy"},
        )
    return _format_paper_detail(paper, s2_data)


async def read_paper(arxiv_id: str, section: str | None = None) -> str:
    """Read paper sections from arxiv HTML; falls back to ar5iv, then abstract."""
    parsed = None
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for base_url in (ARXIV_HTML, AR5IV_HTML):
            try:
                resp = await client.get(f"{base_url}/{arxiv_id}")
                if resp.status_code == 200:
                    parsed = _parse_paper_html(resp.text)
                    if parsed["sections"]:
                        break
                    parsed = None
            except httpx.RequestError:
                continue

    if not parsed or not parsed["sections"]:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{HF_API}/papers/{arxiv_id}")
                resp.raise_for_status()
                paper = resp.json()
            abstract = paper.get("summary", "")
            title = paper.get("title", "")
            return (
                f"# {title}\nhttps://arxiv.org/abs/{arxiv_id}\n\n"
                f"## Abstract\n{abstract}\n\n"
                f"HTML version not available. PDF: https://arxiv.org/pdf/{arxiv_id}"
            )
        except Exception:  # noqa: BLE001
            return f"Could not fetch paper {arxiv_id}. Check the arxiv ID is correct."

    if not section:
        return _format_read_paper_toc(parsed, arxiv_id)
    sec = _find_section(parsed["sections"], section)
    if not sec:
        available = "\n".join(f"- {s['title']}" for s in parsed["sections"])
        return f"Section '{section}' not found. Available:\n{available}"
    return _format_read_paper_section(sec, arxiv_id)


async def citation_graph(
    arxiv_id: str, direction: str = "both", limit: int = DEFAULT_LIMIT,
) -> str:
    """Semantic Scholar references + citations for a paper."""
    limit = min(limit, MAX_LIMIT)
    s2_id = _s2_paper_id(arxiv_id)
    fields = "title,externalIds,year,citationCount,influentialCitationCount,contexts,intents,isInfluential"
    params = {"fields": fields, "limit": limit}

    async with httpx.AsyncClient(timeout=15) as client:
        coros = []
        if direction in ("references", "both"):
            coros.append(_s2_get_json(client, f"/graph/v1/paper/{s2_id}/references", params))
        if direction in ("citations", "both"):
            coros.append(_s2_get_json(client, f"/graph/v1/paper/{s2_id}/citations", params))
        results = await asyncio.gather(*coros, return_exceptions=True)

    refs, cites = None, None
    idx = 0
    if direction in ("references", "both"):
        r = results[idx]
        if isinstance(r, dict):
            refs = r.get("data", [])
        idx += 1
    if direction in ("citations", "both"):
        r = results[idx]
        if isinstance(r, dict):
            cites = r.get("data", [])

    if refs is None and cites is None:
        return f"Could not fetch citations for {arxiv_id}. Paper may not be in Semantic Scholar."

    lines = [f"# Citation Graph for {arxiv_id}", f"https://arxiv.org/abs/{arxiv_id}\n"]
    if refs is not None:
        lines.append(f"## References ({len(refs)})")
        if refs:
            lines.extend(_format_citation_entry(e) for e in refs)
        else:
            lines.append("No references found.")
        lines.append("")
    if cites is not None:
        lines.append(f"## Citations ({len(cites)})")
        if cites:
            lines.extend(_format_citation_entry(e, show_context=True) for e in cites)
        else:
            lines.append("No citations found.")
    return "\n".join(lines)


async def snippet_search(
    query: str,
    limit: int = DEFAULT_LIMIT,
    *,
    date_from: str = "",
    date_to: str = "",
    categories: str | None = None,
    min_citations: int | None = None,
) -> str:
    """Semantic Scholar full-text passage search across 12M+ papers."""
    limit = min(limit, MAX_LIMIT)
    params: dict[str, Any] = {
        "query": query,
        "limit": limit,
        "fields": "title,externalIds,year,citationCount",
    }
    if date_from or date_to:
        params["publicationDateOrYear"] = f"{date_from}:{date_to}"
    if categories:
        params["fieldsOfStudy"] = categories
    if min_citations:
        params["minCitationCount"] = str(min_citations)

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await _s2_request(client, "GET", "/graph/v1/snippet/search", params=params)
        if not resp or resp.status_code != 200:
            return "Snippet search failed. Semantic Scholar may be unavailable."
        data = resp.json()

    snippets = data.get("data") or []
    if not snippets:
        return f"No snippets found for '{query}'."

    lines = [f"# Snippet Search: '{query}'", f"Found {len(snippets)} matching passage(s)\n"]
    for i, item in enumerate(snippets, 1):
        paper = item.get("paper") or {}
        ptitle = paper.get("title") or "(untitled)"
        year = paper.get("year") or "?"
        cites = paper.get("citationCount", 0)
        aid = (paper.get("externalIds") or {}).get("ArXiv", "")
        snip = item.get("snippet") or {}
        text = snip.get("text", "")
        section = snip.get("section") or ""
        lines.append(f"### {i}. {ptitle} ({year}, {cites} cites)")
        if aid:
            lines.append(f"arxiv:{aid}")
        if section:
            lines.append(f"Section: {section}")
        if text:
            lines.append(f"> {_truncate(text, 400)}")
        lines.append("")
    return "\n".join(lines)


async def recommend(
    arxiv_id: str | None = None,
    positive_ids: str | None = None,
    negative_ids: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> str:
    """Recommend similar papers (single arxiv_id, or comma-separated id lists)."""
    limit = min(limit, MAX_LIMIT)
    if not arxiv_id and not positive_ids:
        return "Provide arxiv_id or positive_ids."
    fields = "title,externalIds,year,citationCount,tldr,venue"

    async with httpx.AsyncClient(timeout=15) as client:
        if positive_ids and not arxiv_id:
            pos = [_s2_paper_id(p.strip()) for p in positive_ids.split(",") if p.strip()]
            neg = [
                _s2_paper_id(p.strip()) for p in (negative_ids or "").split(",") if p.strip()
            ]
            resp = await _s2_request(
                client, "POST", "/recommendations/v1/papers/",
                json={"positivePaperIds": pos, "negativePaperIds": neg},
                params={"fields": fields, "limit": limit},
            )
            if not resp or resp.status_code != 200:
                return "Recommendation request failed."
            data = resp.json()
        else:
            data = await _s2_get_json(
                client,
                f"/recommendations/v1/papers/forpaper/{_s2_paper_id(arxiv_id)}",
                {"fields": fields, "limit": limit, "from": "recent"},
            )
            if not data:
                return "Recommendation request failed."

    papers = data.get("recommendedPapers") or []
    if not papers:
        return "No recommendations found."
    return _format_s2_paper_list(
        papers[:limit], f"Recommended papers based on {arxiv_id or positive_ids}",
    )


async def find_datasets(arxiv_id: str, sort: str = "downloads", limit: int = DEFAULT_LIMIT) -> str:
    """HF datasets linked to a paper."""
    limit = min(limit, MAX_LIMIT)
    sort_key = SORT_MAP.get(sort, "downloads")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{HF_API}/datasets",
            params={"filter": f"arxiv:{arxiv_id}", "limit": limit, "sort": sort_key, "direction": -1},
        )
        resp.raise_for_status()
        datasets = resp.json()
    if not datasets:
        return f"No datasets found linked to paper {arxiv_id}."

    lines = [f"# Datasets linked to paper {arxiv_id}", f"Showing {len(datasets)}, sorted by {sort}\n"]
    for i, ds in enumerate(datasets, 1):
        ds_id = ds.get("id", "unknown")
        downloads = ds.get("downloads", 0)
        likes = ds.get("likes", 0)
        desc = _truncate(_clean_description(ds.get("description") or ""), MAX_SUMMARY_LEN)
        tags = [t for t in (ds.get("tags") or []) if not t.startswith(("arxiv:", "region:"))][:5]
        lines.append(f"**{i}. [{ds_id}](https://huggingface.co/datasets/{ds_id})**")
        lines.append(f"   Downloads: {downloads:,} | Likes: {likes}")
        if tags:
            lines.append(f"   Tags: {', '.join(tags)}")
        if desc:
            lines.append(f"   {desc}")
        lines.append("")
    return "\n".join(lines)


async def find_models(arxiv_id: str, sort: str = "downloads", limit: int = DEFAULT_LIMIT) -> str:
    """HF models linked to a paper."""
    limit = min(limit, MAX_LIMIT)
    sort_key = SORT_MAP.get(sort, "downloads")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{HF_API}/models",
            params={"filter": f"arxiv:{arxiv_id}", "limit": limit, "sort": sort_key, "direction": -1},
        )
        resp.raise_for_status()
        models = resp.json()
    if not models:
        return f"No models found linked to paper {arxiv_id}."
    lines = [f"# Models linked to paper {arxiv_id}", f"Showing {len(models)}, sorted by {sort}\n"]
    for i, m in enumerate(models, 1):
        model_id = m.get("id", "unknown")
        downloads = m.get("downloads", 0)
        likes = m.get("likes", 0)
        pipeline = m.get("pipeline_tag") or ""
        library = m.get("library_name") or ""
        lines.append(f"**{i}. [{model_id}](https://huggingface.co/{model_id})**")
        meta = f"   Downloads: {downloads:,} | Likes: {likes}"
        if pipeline:
            meta += f" | Task: {pipeline}"
        if library:
            meta += f" | Library: {library}"
        lines.append(meta)
        lines.append("")
    return "\n".join(lines)


async def find_collections(arxiv_id: str, limit: int = DEFAULT_LIMIT) -> str:
    """HF collections containing a paper."""
    limit = min(limit, MAX_LIMIT)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{HF_API}/collections", params={"paper": arxiv_id})
        resp.raise_for_status()
        collections = resp.json()
    if not collections:
        return f"No collections found containing paper {arxiv_id}."
    collections = collections[:limit]
    lines = [f"# Collections containing paper {arxiv_id}", f"Showing {len(collections)}\n"]
    for i, c in enumerate(collections, 1):
        slug = c.get("slug", "")
        title = c.get("title", "Untitled")
        upvotes = c.get("upvotes", 0)
        owner = c.get("owner", {}).get("name", "")
        desc = _truncate(c.get("description") or "", MAX_SUMMARY_LEN)
        num_items = len(c.get("items", []))
        lines.append(f"**{i}. {title}**")
        lines.append(f"   By: {owner} | Upvotes: {upvotes} | Items: {num_items}")
        lines.append(f"   https://huggingface.co/collections/{slug}")
        if desc:
            lines.append(f"   {desc}")
        lines.append("")
    return "\n".join(lines)


async def find_all_resources(arxiv_id: str, limit: int = DEFAULT_LIMIT) -> str:
    """Parallel datasets + models + collections fetch for a paper."""
    per_cat = min(limit, 10)
    async with httpx.AsyncClient(timeout=15) as client:
        results = await asyncio.gather(
            client.get(f"{HF_API}/datasets", params={
                "filter": f"arxiv:{arxiv_id}", "limit": per_cat,
                "sort": "downloads", "direction": -1,
            }),
            client.get(f"{HF_API}/models", params={
                "filter": f"arxiv:{arxiv_id}", "limit": per_cat,
                "sort": "downloads", "direction": -1,
            }),
            client.get(f"{HF_API}/collections", params={"paper": arxiv_id}),
            return_exceptions=True,
        )

    sections: list[str] = []
    for label, result in zip(("Datasets", "Models", "Collections"), results, strict=True):
        if isinstance(result, Exception):
            sections.append(f"## {label}\nError: {result}")
            continue
        items = result.json()[:per_cat]
        if not items:
            sections.append(f"## {label}\nNone found")
            continue
        sub = [f"## {label} ({len(items)})"]
        for it in items:
            if label == "Datasets":
                sub.append(f"- **{it.get('id', '?')}** ({it.get('downloads', 0):,} downloads)")
            elif label == "Models":
                pipeline = it.get("pipeline_tag") or ""
                suffix = f" ({pipeline})" if pipeline else ""
                sub.append(
                    f"- **{it.get('id', '?')}** ({it.get('downloads', 0):,} downloads){suffix}",
                )
            else:  # Collections
                title = it.get("title", "Untitled")
                owner = it.get("owner", {}).get("name", "")
                upvotes = it.get("upvotes", 0)
                sub.append(f"- **{title}** by {owner} ({upvotes} upvotes)")
        sections.append("\n".join(sub))

    header = f"# Resources linked to paper {arxiv_id}\nhttps://huggingface.co/papers/{arxiv_id}\n"
    return header + "\n\n".join(sections)
