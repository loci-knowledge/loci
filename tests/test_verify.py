"""Verifier unit tests.

The LLM-call path is tested implicitly via `verify()` returning unknown
verdicts when no API key is configured. The pure parsing logic
(`split_claims`) is fully covered by direct tests.
"""

from __future__ import annotations

from loci.verify import split_claims, verify


def test_split_claims_skips_sentences_without_handles():
    md = (
        "This sentence has no citation. "
        "But this one does [C1]. "
        "And this one cites two [C2][C3]."
    )
    units = split_claims(md)
    assert len(units) == 2
    assert units[0].handles == ["C1"]
    assert units[1].handles == ["C2", "C3"]


def test_split_claims_normalises_handle_case():
    md = "Lower-case handle [c4]."
    units = split_claims(md)
    assert units[0].handles == ["C4"]


def test_split_claims_multiline():
    md = (
        "Paragraph one mentions a fact [C1].\n\n"
        "Paragraph two also makes a claim [C2]. "
        "And another sentence [C3]!"
    )
    units = split_claims(md)
    assert len(units) == 3
    assert units[0].handles == ["C1"]
    assert units[1].handles == ["C2"]
    assert units[2].handles == ["C3"]


def test_verify_returns_unknown_when_chunk_missing(monkeypatch):
    """A handle present in the draft but missing from the chunks dict gets
    verdict='unknown' without an LLM call."""
    md = "First claim [C1]. Second claim [C2]."
    # Only C1 has a chunk; C2 should come back as unknown.
    chunks = {"C1": "The first claim is supported by this text."}

    # Force build_agent to raise so we know no LLM call is made.
    from loci import verify as verify_mod
    from loci.llm import LLMNotConfiguredError

    def _raise(*a, **kw):
        raise LLMNotConfiguredError("test stub: no LLM")

    monkeypatch.setattr(verify_mod, "build_agent", _raise)

    res = verify(md, chunks)
    handles = {v.handle for v in res.verdicts}
    # Both handles should appear in the result.
    assert "C1" in handles
    assert "C2" in handles
    # C2 (no chunk) is verdict=unknown; C1 may also be unknown if LLM
    # didn't run, which is what we expect with the stub.
    by_handle = {v.handle: v for v in res.verdicts}
    assert by_handle["C2"].verdict == "unknown"
    assert "no chunk-level span" in by_handle["C2"].reason.lower()


def test_verify_no_claims_returns_empty():
    md = "A draft with no citations at all."
    res = verify(md, {})
    assert res.verdicts == []
    assert res.error is None
