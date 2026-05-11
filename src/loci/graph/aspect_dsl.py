"""Controlled-NL DSL for aspect propositions.

Aspects move from flat strings to typed propositions:

    topic [as kind] [role target] [key=value ...]

Examples:
    literate-programming
    literate-programming as methodology
    reproducibility as critique critiques frequentist-statistics
    dropout as technique applies_to=overfitting scope=narrow
    prequential-validation as methodology extends cross-validation

The DSL is deliberately small:
- `kind`  is a controlled+extensible set (methodology, technique, …).
- `role`  is a CLOSED set of ~10 verbs (critiques, supports, extends, …).
  Closed because role drives automatic aspect_edges materialization.
- Anything after kind+role that is `key=value` becomes a modifier.
- Any parse failure degrades gracefully to Proposition(topic=slugified_input).

Round-trip invariant: parse(render(p)) == p  for all well-formed p.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Controlled sets
# ---------------------------------------------------------------------------

ROLES: frozenset[str] = frozenset(
    {
        "exemplifies",
        "critiques",
        "supports",
        "extends",
        "reviews",
        "rejects",
        "applies",
        "introduces",
        "rebuts",
        "grounds",
    }
)

GLOBAL_KINDS: frozenset[str] = frozenset(
    {
        "methodology",
        "technique",
        "concept",
        "tool",
        "resource",
        "critique",
        "reference",
        "claim",
        "dataset",
    }
)

# role → (aspect_edge_type, default_weight)
ROLE_TO_EDGE: dict[str, tuple[str, float]] = {
    "critiques":   ("opposite_of", 1.0),
    "rebuts":      ("opposite_of", 1.0),
    "rejects":     ("opposite_of", 0.7),
    "extends":     ("parent_of",   1.0),   # target is parent of topic
    "exemplifies": ("parent_of",   0.8),
    "supports":    ("related_to",  1.0),
    "applies":     ("related_to",  0.6),
    "introduces":  ("related_to",  0.5),
    "reviews":     ("related_to",  0.4),
    "grounds":     ("related_to",  0.5),
}

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*$")
_MODIFIER_RE = re.compile(r"^([a-z0-9_\-]+)=(.+)$")


# ---------------------------------------------------------------------------
# Proposition dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Proposition:
    """A structured aspect proposition.

    `topic`  is the canonical head slug (always present).
    `kind`   is an optional controlled type (methodology, technique, …).
    `role`   is an optional closed-set verb (critiques, extends, …).
    `target` is the slug of another topic this proposition relates to via role.
    `modifiers` captures remaining key=value pairs.

    A flat legacy label becomes Proposition(topic=label) with all others None.
    """

    topic: str
    kind: str | None = None
    role: str | None = None
    target: str | None = None
    modifiers: dict[str, str] | None = field(default=None, compare=True, hash=False)

    def __hash__(self) -> int:  # noqa: D105
        mods = tuple(sorted(self.modifiers.items())) if self.modifiers else ()
        return hash((self.topic, self.kind, self.role, self.target, mods))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Proposition):
            return NotImplemented
        return (
            self.topic == other.topic
            and self.kind == other.kind
            and self.role == other.role
            and self.target == other.target
            and (self.modifiers or {}) == (other.modifiers or {})
        )

    @property
    def is_flat(self) -> bool:
        """True when only topic is populated (degenerate / legacy proposition)."""
        return self.kind is None and self.role is None and self.target is None and not self.modifiers


# ---------------------------------------------------------------------------
# Parser helpers
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    """Convert arbitrary text to a valid slug (lowercase, hyphens)."""
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s or "unknown"


def _is_slug(token: str) -> bool:
    return bool(_SLUG_RE.match(token))


def _parse_modifier(token: str) -> tuple[str, str] | None:
    m = _MODIFIER_RE.match(token)
    return (m.group(1), m.group(2)) if m else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse(text: str) -> Proposition:
    """Parse a CNL proposition string into a Proposition.

    Grammar (left-to-right, each part optional except topic):
        topic [as kind] [role target] [key=value ...]

    Any unrecognised structure degrades: bad role → kept as topic-only;
    bad kind → ignored; bad modifier → ignored with a warning.
    """
    tokens = text.strip().split()
    if not tokens:
        return Proposition(topic="unknown")

    # 1. Topic (first token)
    topic = _slugify(tokens[0])
    pos = 1

    # 2. Optional "as kind"
    kind: str | None = None
    if pos + 1 < len(tokens) and tokens[pos] == "as":
        candidate_kind = tokens[pos + 1].lower()
        # Accept any slug as kind (project-extensible); warn if not in global set
        if _is_slug(candidate_kind):
            kind = candidate_kind
            if candidate_kind not in GLOBAL_KINDS:
                log.debug("aspect_dsl.parse: non-global kind %r accepted", candidate_kind)
            pos += 2

    # 3. Optional role + target
    role: str | None = None
    target: str | None = None
    if pos < len(tokens) and tokens[pos] in ROLES:
        role = tokens[pos]
        pos += 1
        if pos < len(tokens) and _is_slug(tokens[pos]) and "=" not in tokens[pos]:
            target = _slugify(tokens[pos])
            pos += 1
        else:
            # role without a valid target: drop the role
            log.debug("aspect_dsl.parse: role %r has no valid target; ignoring role", role)
            role = None

    # 4. Modifiers (key=value)
    modifiers: dict[str, str] = {}
    while pos < len(tokens):
        mod = _parse_modifier(tokens[pos])
        if mod is None:
            log.debug("aspect_dsl.parse: skipping unrecognised token %r", tokens[pos])
        else:
            modifiers[mod[0]] = mod[1]
        pos += 1

    return Proposition(
        topic=topic,
        kind=kind,
        role=role,
        target=target,
        modifiers=modifiers or None,
    )


def render(prop: Proposition) -> str:
    """Render a Proposition back to its canonical CNL string.

    Inverse of parse for well-formed propositions.
    """
    parts = [prop.topic]
    if prop.kind:
        parts += ["as", prop.kind]
    if prop.role:
        parts.append(prop.role)
        if prop.target:
            parts.append(prop.target)
    if prop.modifiers:
        for k, v in sorted(prop.modifiers.items()):
            parts.append(f"{k}={v}")
    return " ".join(parts)


def match(prop: Proposition, query: "Query") -> bool:  # noqa: F821
    """Test whether prop satisfies all non-None fields in query.

    Used for post-fetch filtering in aspect_overlap_rank.
    Fuzzy (~) topic matching is handled by the SQL layer; this checks typed fields.
    """
    from loci.retrieve.query_cnl import Query  # noqa: PLC0415 — lazy to avoid circular

    if query.kind is not None and prop.kind != query.kind:
        return False
    if query.role is not None and prop.role != query.role:
        return False
    if query.target is not None:
        if prop.target is None or query.target not in prop.target:
            return False
    return True


# ---------------------------------------------------------------------------
# Edge spec: what to_aspect_edges returns
# ---------------------------------------------------------------------------


@dataclass
class AspectEdgeSpec:
    """A pending aspect-to-aspect edge to be materialised by the caller."""

    src_topic: str     # aspect_vocab.topic
    dst_topic: str     # aspect_vocab.topic (the target)
    edge_type: str
    weight: float
    project_id: str | None
    deterministic_id: str


def to_aspect_edges(
    prop: Proposition,
    *,
    project_id: str,
    resource_id: str,
    aspect_id: str,
) -> list[AspectEdgeSpec]:
    """Derive zero or more aspect-to-aspect edges from a typed proposition.

    Only propositions with a role AND a target produce edges. The caller is
    responsible for resolving target topics to aspect_vocab IDs and writing to
    aspect_edges.

    Edge direction: depends on role semantics:
    - extends/exemplifies: target is PARENT of topic → edge (target, topic, parent_of)
    - critiques/rebuts/rejects: topic OPPOSES target → edge (topic, target, opposite_of)
    - supports/applies/introduces/reviews/grounds: topic RELATES to target → (topic, target, related_to)

    Deterministic ID: sha256("role|project|resource|aspect|role") so re-materialisation
    is idempotent (same inputs → same ID → UPDATE or no-op, not a duplicate).
    """
    if prop.role is None or prop.target is None:
        return []

    edge_type, weight = ROLE_TO_EDGE[prop.role]

    raw = f"role|{project_id}|{resource_id}|{aspect_id}|{prop.role}"
    det_id = hashlib.sha256(raw.encode()).hexdigest()[:32]

    # For parent_of semantics: edge goes from target (parent) to topic (child)
    if edge_type == "parent_of":
        src_topic = prop.target
        dst_topic = prop.topic
    else:
        src_topic = prop.topic
        dst_topic = prop.target

    return [
        AspectEdgeSpec(
            src_topic=src_topic,
            dst_topic=dst_topic,
            edge_type=edge_type,
            weight=weight,
            project_id=project_id,
            deterministic_id=det_id,
        )
    ]


def role_edge_det_id(project_id: str, resource_id: str, aspect_id: str, role: str) -> str:
    """Return the deterministic edge ID for a (project, resource, aspect, role) tuple.

    Used to delete stale role-derived edges when the proposition is edited.
    """
    raw = f"role|{project_id}|{resource_id}|{aspect_id}|{role}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]
