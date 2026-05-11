"""CNL query parser for loci_recall / loci_browse.

The query surface is a tiny Datalog-flavoured prefix notation:

    ?topic=reproducibility ?kind=methodology
    ?role=critiques ?target~bayesian
    ?kind=methodology dropout

- Clauses beginning with `?` are structured field filters.
- `=` means exact match; `~` means fuzzy (LIKE %value%).
- Remaining bare tokens form the free-text part (BM25 / ANN / HyDE).
- No `?` at all → pure free-text (same as today).

Usage::

    free_text, query = split_query("?role=critiques reproducibility")
    # free_text = "reproducibility", query = Query(role="critiques")

    params = query.to_sql_params()
    # consumed by aspect_overlap_rank(conn, project_id, query=query, ...)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_CLAUSE_RE = re.compile(r"^\?([a-z_]+)([=~])(.+)$")


@dataclass
class Query:
    """Structured aspect query parsed from CNL clauses (?key=value).

    All fields are optional. None means "no constraint on this dimension".
    `topic_fuzzy=True` when the topic clause used `~` (fuzzy LIKE) rather
    than `=` (exact match).
    """

    topic: str | None = None
    topic_fuzzy: bool = False
    kind: str | None = None
    role: str | None = None
    target: str | None = None
    target_fuzzy: bool = False
    modifiers: dict[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """True when no structured constraints are set."""
        return (
            self.topic is None
            and self.kind is None
            and self.role is None
            and self.target is None
            and not self.modifiers
        )

    def to_sql_params(self) -> dict:
        """Return a dict of named SQL params for aspect_overlap_rank.

        Keys match the placeholders used in concept_expand.aspect_overlap_rank.
        """
        topic_pat: str | None = None
        if self.topic is not None:
            topic_pat = f"%{self.topic}%" if self.topic_fuzzy else self.topic

        target_pat: str | None = None
        if self.target is not None:
            target_pat = f"%{self.target}%" if self.target_fuzzy else self.target

        return {
            "q_topic_pat": topic_pat,
            "q_topic_exact": self.topic if not self.topic_fuzzy else None,
            "q_kind": self.kind,
            "q_role": self.role,
            "q_target_slug": target_pat,
        }


def parse_query(text: str) -> Query:
    """Parse a sequence of ?clause tokens into a Query.

    Unknown field names are stored in Query.modifiers for forward-compat.
    Non-clause tokens are silently skipped (they belong in the free-text part).
    """
    q = Query()
    for token in text.strip().split():
        m = _CLAUSE_RE.match(token)
        if m is None:
            continue
        key, op, val = m.group(1), m.group(2), m.group(3).lower()
        fuzzy = op == "~"
        if key == "topic":
            q.topic = val
            q.topic_fuzzy = fuzzy
        elif key == "kind":
            q.kind = val
        elif key == "role":
            q.role = val
        elif key == "target":
            q.target = val
            q.target_fuzzy = fuzzy
        else:
            q.modifiers[key] = val
    return q


def split_query(text: str) -> tuple[str, Query]:
    """Split a raw query string into (free_text, Query).

    ?clause tokens are extracted into a Query; everything else becomes
    free_text for BM25 / ANN / HyDE.

    Examples::

        split_query("?role=critiques reproducibility")
        # ("reproducibility", Query(role="critiques"))

        split_query("attention mechanism")
        # ("attention mechanism", Query())
    """
    clause_tokens: list[str] = []
    free_tokens: list[str] = []
    for token in text.strip().split():
        if token.startswith("?"):
            clause_tokens.append(token)
        else:
            free_tokens.append(token)

    query = parse_query(" ".join(clause_tokens))
    free_text = " ".join(free_tokens)
    return free_text, query
