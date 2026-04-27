"""Research sub-agent.

A pydantic-ai agent that searches papers, optionally runs code in an HF
Spaces sandbox, and saves artifacts (paper notes, code, summaries) to a
local directory. The artifacts are then ingested back into loci's graph
by the autoresearch job handler.

Public API:
    run_research(query, *, output_dir, hf_owner=None, sandbox_hardware="cpu-basic",
                 max_iterations=30, project_profile_md="") -> ResearchReport

ResearchReport carries:
    summary_md      — narrative recipe table / takeaways (also saved as SUMMARY.md)
    artifacts       — list of relative paths under output_dir
    sandbox_url     — URL of the HF Space if one was created (else None)
    used_papers     — arxiv_ids the agent cited in summary
    iterations      — turns taken
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.messages import FunctionToolCallEvent, FunctionToolResultEvent
from pydantic_ai.usage import UsageLimits

from loci.config import get_settings
from loci.llm.agent import LLMNotConfiguredError, build_agent
from loci.research import papers as papers_mod
from loci.research.sandbox import Sandbox, SandboxNotConfigured

log = logging.getLogger(__name__)

_TOOL_ICONS: dict[str, str] = {
    "search_papers": "🔍",
    "trending_papers": "📈",
    "paper_details": "📄",
    "read_paper": "📖",
    "citation_graph": "🕸",
    "snippet_search": "🔎",
    "recommend": "💡",
    "find_datasets": "🗂",
    "find_models": "🤖",
    "find_collections": "📚",
    "find_all_resources": "🔗",
    "save_paper_note": "💾",
    "save_note": "📝",
    "sandbox_bash": "⚡",
    "sandbox_read": "📂",
    "sandbox_write": "✏",
    "sandbox_edit": "✂",
    "save_code": "🖥",
}


def _step_label(tool_name: str, args: object) -> str:
    icon = _TOOL_ICONS.get(tool_name, "⚙")
    d = args if isinstance(args, dict) else {}
    if tool_name in ("search_papers", "snippet_search"):
        q = str(d.get("query", ""))[:60]
        return f"{icon} {tool_name.replace('_', ' ')}: {q}"
    if tool_name in ("paper_details", "citation_graph", "recommend",
                     "find_datasets", "find_models", "find_collections", "find_all_resources"):
        arxiv = d.get("arxiv_id") or d.get("positive_ids") or ""
        return f"{icon} {tool_name.replace('_', ' ')}: {str(arxiv)[:40]}"
    if tool_name == "read_paper":
        arxiv = d.get("arxiv_id", "")
        sec = d.get("section", "")
        suffix = f" §{sec}" if sec else ""
        return f"{icon} reading {arxiv}{suffix}"
    if tool_name in ("save_paper_note", "save_note"):
        key = "arxiv_id" if tool_name == "save_paper_note" else "filename"
        return f"{icon} saving: {str(d.get(key, ''))[:40]}"
    if tool_name == "sandbox_bash":
        cmd = str(d.get("command", ""))[:50]
        return f"{icon} exec: {cmd}"
    return f"{icon} {tool_name}"


RESEARCH_SYSTEM_PROMPT = """\
You are a research sub-agent. The user has a project and wants you to investigate
a question by reading the literature, optionally running code, and saving artifacts
that future retrieval can route to.

You have these tools:
  PAPER DISCOVERY (always available):
    - search_papers(query, ...)      — HF Papers (default) or Semantic Scholar (with filters)
    - paper_details(arxiv_id)        — metadata + abstract + S2 enrichment
    - read_paper(arxiv_id, section?) — full sections of a paper from arxiv HTML
    - citation_graph(arxiv_id, ...)  — references + citations with influence flags
    - snippet_search(query, ...)     — full-text passage search across 12M+ papers
    - recommend(arxiv_id, ...)       — find similar papers
    - find_datasets / find_models / find_collections / find_all_resources
    - trending_papers(date?, query?) — daily trending on HF

  ARTIFACTS (always available):
    - save_paper_note(arxiv_id, body_md, title?) — save a paper's content as an artifact
    - save_note(filename, body_md)               — save an arbitrary research note

  CODE EXECUTION (only when sandbox is configured — otherwise these tools are absent):
    - sandbox_bash(command)               — run a shell command in an HF Space
    - sandbox_read(path)                  — read a file from the sandbox
    - sandbox_write(path, content)        — write a file
    - sandbox_edit(path, old, new)        — targeted edit
    - save_code(filename, body)           — save code from the sandbox as an artifact

WORKFLOW
  1. Start by searching for relevant papers (search_papers OR trending_papers OR
     snippet_search). Read their abstracts; pick 2–5 that look most relevant.
  2. For each promising paper, fetch full details with paper_details, then read
     methodology sections (typically section 3–5) with read_paper. If the paper
     is highly relevant, follow citation_graph to find prior + downstream work.
  3. Save the paper's relevant content with save_paper_note(arxiv_id, body_md).
     The body_md should include: title, arxiv URL, key claim(s), method summary,
     and direct quotes / section pointers. NOT a full paraphrase — the artifact
     should anchor back to the source.
  4. If the user's query benefits from code (e.g. they want a small experiment,
     a dataset inspection, a reference implementation), use the sandbox tools.
     Save any code you write or run with save_code(filename, body). Save its
     output with save_note(filename, body) so the result is retrievable later.
  5. Stop when you have enough material to answer the user's query, or after
     max_iterations.

OUTPUT
  Return a ResearchReport with:
    - summary_md: a tight markdown summary of what you found. Lead with the
      direct answer to the user's query, then a recipe / table of artifacts:
      paper title → arxiv_id → relevance to the project.
    - artifacts: every relative path under the output dir you wrote.
    - used_papers: arxiv_ids that materially informed the summary.

RULES
  - Never fabricate arxiv ids. If a search returns nothing, say so.
  - Do not save full PDFs — save markdown notes that point at source URLs and
    quote the parts that matter.
  - Prefer a few deeply-read papers over many shallow ones.
  - Be concise in summary_md (≤1500 words).
"""


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------


class ResearchOutput(BaseModel):
    """Structured output the LLM produces."""

    summary_md: str = Field(description="Markdown summary of findings; ≤1500 words.")
    used_papers: list[str] = Field(
        default_factory=list,
        description="arxiv IDs that materially informed the summary.",
    )


@dataclass
class ResearchReport:
    summary_md: str
    artifacts: list[str] = field(default_factory=list)
    used_papers: list[str] = field(default_factory=list)
    sandbox_url: str | None = None
    iterations: int = 0
    skipped: bool = False
    skip_reason: str = ""


# ---------------------------------------------------------------------------
# Artifact recorder
# ---------------------------------------------------------------------------


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str, default: str = "note.md") -> str:
    name = _SAFE_NAME.sub("-", name).strip("-.")
    if not name:
        return default
    return name


@dataclass
class _Recorder:
    """Mutable bag of artifacts the agent has saved (relative paths)."""

    output_dir: Path
    artifacts: list[str] = field(default_factory=list)

    def write(self, rel_path: str, body: str) -> str:
        target = self.output_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        if rel_path not in self.artifacts:
            self.artifacts.append(rel_path)
        return rel_path


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def run_research(
    query: str,
    *,
    output_dir: Path,
    hf_owner: str | None = None,
    hf_token: str | None = None,
    sandbox_hardware: str = "cpu-basic",
    sandbox_template: str = "burtenshaw/sandbox",
    enable_sandbox: bool = True,
    max_iterations: int = 30,
    project_profile_md: str = "",
    model_spec: str | None = None,
    on_step: Callable[[str, str], None] | None = None,
) -> ResearchReport:
    """Run the research sub-agent. Synchronous facade over an async agent run.

    Parameters:
        query:               The research task.
        output_dir:          Directory to write artifacts to. Created if absent.
        hf_owner:            HF username/org under which to spin up the sandbox.
                             Required when `enable_sandbox=True`.
        hf_token:            HF API token. Falls back to HF_TOKEN env var.
        sandbox_hardware:    HF Spaces hardware tier.
        sandbox_template:    Template Space to duplicate.
        enable_sandbox:      Whether to spin up an HF Space sandbox.
        max_iterations:      Hard cap on agent loop iterations.
        project_profile_md:  Project profile to ground the research in.
        model_spec:          Override Settings.research_model.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    spec = model_spec or getattr(settings, "research_model", settings.interpretation_model)

    try:
        agent = build_agent(spec, instructions=RESEARCH_SYSTEM_PROMPT, output_type=ResearchOutput)
    except LLMNotConfiguredError as exc:
        log.info("research: %s; skipping", exc)
        return ResearchReport(
            summary_md="", skipped=True, skip_reason=str(exc),
        )

    sandbox: Sandbox | None = None
    if enable_sandbox and hf_owner:
        try:
            sandbox = Sandbox.create(
                owner=hf_owner,
                template=sandbox_template,
                hardware=sandbox_hardware,
                token=hf_token,
                log=lambda m: log.info("sandbox: %s", m),
            )
        except (SandboxNotConfigured, Exception) as exc:  # noqa: BLE001
            log.warning("research: sandbox creation failed: %s", exc)
            sandbox = None

    recorder = _Recorder(output_dir=output_dir)
    _register_paper_tools(agent, recorder)
    if sandbox is not None:
        _register_sandbox_tools(agent, sandbox, recorder)

    user_msg = (
        f"RESEARCH TASK: {query}\n\n"
        + (f"PROJECT PROFILE:\n{project_profile_md}\n\n" if project_profile_md else "")
        + f"OUTPUT DIRECTORY: {output_dir}\n"
        + (
            f"SANDBOX: {sandbox.url} (cpu-basic). Use sandbox tools to run code.\n"
            if sandbox is not None
            else "SANDBOX: not available; only paper search + artifact tools.\n"
        )
        + f"MAX ITERATIONS: {max_iterations}\n"
    )

    log.info(
        "research: starting agent loop model=%s query=%r max_iter=%d sandbox=%s",
        spec, query[:80], max_iterations, "yes" if sandbox is not None else "no",
    )

    async def _on_events(_ctx: object, event_stream: object) -> None:
        async for event in event_stream:  # type: ignore[union-attr]
            if isinstance(event, FunctionToolCallEvent):
                brief = str(event.part.args)[:120]
                log.info("research: → %s  %s", event.part.tool_name, brief)
                if on_step is not None:
                    label = _step_label(event.part.tool_name, event.part.args)
                    on_step(event.part.tool_name, label)
            elif isinstance(event, FunctionToolResultEvent):
                body = str(getattr(event.result, "content", ""))[:80]
                log.info("research: ←  %s", body)

    try:
        result = agent.run_sync(
            user_msg,
            usage_limits=UsageLimits(request_limit=max_iterations),
            event_stream_handler=_on_events,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("research: agent run failed")
        import contextlib
        if sandbox is not None:
            with contextlib.suppress(Exception):
                sandbox.delete()
        return ResearchReport(
            summary_md=f"Research run failed: {exc}",
            artifacts=recorder.artifacts,
            sandbox_url=sandbox.url if sandbox is not None else None,
            skipped=True,
            skip_reason=str(exc),
        )

    usage = result.usage()
    log.info(
        "research: done requests=%s tokens_in=%s tokens_out=%s artifacts=%d",
        usage.requests, usage.input_tokens, usage.output_tokens, len(recorder.artifacts),
    )

    output: ResearchOutput = result.output

    # Save the final summary as an artifact too — that's the locus's anchor.
    summary_path = recorder.write("SUMMARY.md", output.summary_md)
    if summary_path not in recorder.artifacts:
        recorder.artifacts.append(summary_path)

    sandbox_url = sandbox.url if sandbox is not None else None
    if sandbox is not None:
        try:
            sandbox.delete()
        except Exception as exc:  # noqa: BLE001
            log.warning("research: sandbox cleanup failed: %s", exc)

    return ResearchReport(
        summary_md=output.summary_md,
        artifacts=recorder.artifacts,
        used_papers=list(output.used_papers),
        sandbox_url=sandbox_url,
        iterations=usage.requests or 0,
    )


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def _register_paper_tools(agent: Agent, recorder: _Recorder) -> None:
    """Wire the paper-discovery + artifact-writing tools onto the agent."""

    @agent.tool_plain
    async def search_papers(
        query: str, limit: int = 10,
        date_from: str = "", date_to: str = "",
        categories: str = "", min_citations: int = 0, sort_by: str = "",
    ) -> str:
        """Search papers. Filters route to Semantic Scholar; otherwise HF Papers."""
        return await papers_mod.search(
            query, limit,
            date_from=date_from, date_to=date_to,
            categories=categories or None,
            min_citations=min_citations or None,
            sort_by=sort_by or None,
        )

    @agent.tool_plain
    async def trending_papers(date: str = "", query: str = "", limit: int = 10) -> str:
        """Daily trending papers from HuggingFace, optional keyword filter."""
        return await papers_mod.trending(
            date=date or None, query=query or None, limit=limit,
        )

    @agent.tool_plain
    async def paper_details(arxiv_id: str) -> str:
        """Full paper metadata: HuggingFace + Semantic Scholar."""
        return await papers_mod.paper_details(arxiv_id)

    @agent.tool_plain
    async def read_paper(arxiv_id: str, section: str = "") -> str:
        """Read paper sections from arxiv HTML. Omit `section` for TOC + abstract."""
        return await papers_mod.read_paper(arxiv_id, section=section or None)

    @agent.tool_plain
    async def citation_graph(arxiv_id: str, direction: str = "both", limit: int = 10) -> str:
        """References + citations for a paper. direction: citations|references|both."""
        return await papers_mod.citation_graph(arxiv_id, direction=direction, limit=limit)

    @agent.tool_plain
    async def snippet_search(query: str, limit: int = 10) -> str:
        """Semantic Scholar full-text passage search across 12M+ papers."""
        return await papers_mod.snippet_search(query, limit=limit)

    @agent.tool_plain
    async def recommend(
        arxiv_id: str = "", positive_ids: str = "", negative_ids: str = "", limit: int = 10,
    ) -> str:
        """Recommend similar papers. Single arxiv_id, or comma-separated id lists."""
        return await papers_mod.recommend(
            arxiv_id=arxiv_id or None,
            positive_ids=positive_ids or None,
            negative_ids=negative_ids or None,
            limit=limit,
        )

    @agent.tool_plain
    async def find_datasets(arxiv_id: str, sort: str = "downloads", limit: int = 10) -> str:
        """HuggingFace datasets linked to a paper."""
        return await papers_mod.find_datasets(arxiv_id, sort=sort, limit=limit)

    @agent.tool_plain
    async def find_models(arxiv_id: str, sort: str = "downloads", limit: int = 10) -> str:
        """HuggingFace models linked to a paper."""
        return await papers_mod.find_models(arxiv_id, sort=sort, limit=limit)

    @agent.tool_plain
    async def find_collections(arxiv_id: str, limit: int = 10) -> str:
        """HuggingFace collections containing a paper."""
        return await papers_mod.find_collections(arxiv_id, limit=limit)

    @agent.tool_plain
    async def find_all_resources(arxiv_id: str, limit: int = 10) -> str:
        """Parallel datasets + models + collections fetch for a paper."""
        return await papers_mod.find_all_resources(arxiv_id, limit=limit)

    @agent.tool_plain
    def save_paper_note(arxiv_id: str, body_md: str, title: str = "") -> str:
        """Save a markdown note about a paper. Lands as a `papers/<arxiv_id>.md` artifact."""
        clean_id = _safe_filename(arxiv_id, default="paper.md")
        if not clean_id.endswith(".md"):
            clean_id = clean_id + ".md"
        rel = f"papers/{clean_id}"
        header = f"# {title or arxiv_id}\n\narxiv_id: {arxiv_id}\nhttps://arxiv.org/abs/{arxiv_id}\n\n"
        recorder.write(rel, header + body_md)
        return f"saved to: {rel}"

    @agent.tool_plain
    def save_note(filename: str, body_md: str) -> str:
        """Save an arbitrary research note. Lands as `notes/<filename>` artifact."""
        clean = _safe_filename(filename, default="note.md")
        if "." not in clean:
            clean += ".md"
        rel = f"notes/{clean}"
        recorder.write(rel, body_md)
        return f"saved to: {rel}"


def _register_sandbox_tools(agent: Agent, sandbox: Sandbox, recorder: _Recorder) -> None:
    """Wire sandbox + code-saving tools onto the agent."""

    @agent.tool_plain
    def sandbox_bash(command: str, work_dir: str = "/app", timeout: int = 240) -> str:
        """Run a shell command inside the HF Spaces sandbox; returns stdout/stderr."""
        return str(sandbox.bash(command, work_dir=work_dir, timeout=timeout))

    @agent.tool_plain
    def sandbox_read(path: str, offset: int = 0, limit: int = 0) -> str:
        """Read a file from the sandbox. Use offset/limit for large files (1-based)."""
        return str(sandbox.read(path, offset=offset or None, limit=limit or None))

    @agent.tool_plain
    def sandbox_write(path: str, content: str) -> str:
        """Write a file in the sandbox (creates parents). Read it first if it exists."""
        return str(sandbox.write(path, content))

    @agent.tool_plain
    def sandbox_edit(
        path: str, old_str: str, new_str: str,
        replace_all: bool = False, mode: str = "replace",
    ) -> str:
        """Edit a file in the sandbox. Read it first; old_str must be unique unless replace_all."""
        return str(sandbox.edit(path, old_str, new_str, replace_all=replace_all, mode=mode))

    @agent.tool_plain
    def save_code(filename: str, body: str) -> str:
        """Save code (typically pulled from the sandbox) as a `code/<filename>` artifact."""
        clean = _safe_filename(filename, default="snippet.txt")
        rel = f"code/{clean}"
        recorder.write(rel, body)
        return f"saved to: {rel}"
