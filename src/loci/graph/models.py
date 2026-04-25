"""Pydantic models for the graph layer.

These mirror the SQL schema in `loci/db/migrations/0001_initial.sql`. They are
the read/write shape used by the API layer and the repositories. Keeping them
strict (using `Literal` enums and tight constraints) catches a lot of bugs that
would otherwise only surface at SQL CHECK time.

A note on `Node` vs `RawNode` / `InterpretationNode`: the SQL schema stores
the base columns in `nodes` and the kind-specific columns in side tables. The
Python models follow the same split: `Node` is the base shape, and the two
subtypes inherit. We expose discriminated-union types via `kind` for the API.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

import ulid
from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Type aliases (Literal unions) — keep these in lock-step with the SQL CHECKs
# in 0001_initial.sql. If you add a value here, add it there.
# ---------------------------------------------------------------------------

NodeKind = Literal["raw", "interpretation"]
NodeStatus = Literal["proposed", "live", "dirty", "stale", "dismissed"]

RawSubkind = Literal["pdf", "md", "code", "html", "transcript", "txt", "image"]
InterpretationSubkind = Literal[
    "philosophy", "pattern", "tension", "decision", "question",
    "touchstone", "experiment", "metaphor",
]
Subkind = RawSubkind | InterpretationSubkind

InterpretationOrigin = Literal[
    "user_correction", "user_pin", "user_summary",
    "user_explicit_create", "proposal_accepted",
    "agent_synthesis",   # written autonomously by the interpreter (Phase F)
]

EdgeType = Literal[
    "cites", "reinforces", "contradicts", "extends",
    "specializes", "generalizes", "aliases", "co_occurs",
]

EdgeCreator = Literal["user", "system", "proposal_accepted"]

Role = Literal["included", "excluded", "pinned"]

# Edge type metadata. Two questions any caller may ask:
#   - is this type symmetric? (then we auto-create the reciprocal)
#   - what's the inverse? (only `specializes` ↔ `generalizes`)
SYMMETRIC_EDGE_TYPES: frozenset[EdgeType] = frozenset({
    "reinforces", "contradicts", "aliases", "co_occurs",
})
EDGE_INVERSES: dict[EdgeType, EdgeType] = {
    "specializes": "generalizes",
    "generalizes": "specializes",
}


def new_id() -> str:
    """Generate a fresh ULID. 26 chars, time-sortable, base32."""
    return str(ulid.new())


def now_iso() -> str:
    """ISO 8601 UTC timestamp matching SQLite's strftime('%Y-%m-%dT%H:%M:%fZ', 'now')."""
    # microseconds → milliseconds (3 digits) to match the schema's %f format.
    dt = datetime.now(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


class Node(BaseModel):
    """Base node shape. The kind/subkind discriminates the read interpretation.

    For writes, prefer constructing `RawNode` or `InterpretationNode` so the
    invariants (e.g. raw subkind ↔ raw kind) are enforced by Pydantic.
    """
    model_config = ConfigDict(frozen=False)

    id: str = Field(default_factory=new_id)
    kind: NodeKind
    subkind: Subkind
    title: str
    body: str = ""
    tags: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    last_accessed_at: str | None = None
    access_count: int = 0
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0
    status: NodeStatus = "live"

    @field_validator("title")
    @classmethod
    def _title_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Node.title must be non-empty")
        return v


class RawNode(Node):
    kind: Literal["raw"] = "raw"
    subkind: RawSubkind
    content_hash: str
    canonical_path: str
    mime: str
    size_bytes: int = Field(ge=0)
    source_of_truth: bool = True


class InterpretationNode(Node):
    kind: Literal["interpretation"] = "interpretation"
    subkind: InterpretationSubkind
    origin: InterpretationOrigin
    origin_session_id: str | None = None
    origin_response_id: str | None = None


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------


class Edge(BaseModel):
    id: str = Field(default_factory=new_id)
    src: str
    dst: str
    type: EdgeType
    weight: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0
    created_at: str = Field(default_factory=now_iso)
    created_by: EdgeCreator = "user"
    symmetric: bool = False  # filled by the repo from SYMMETRIC_EDGE_TYPES

    @field_validator("src", "dst")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not v or len(v) != 26:
            raise ValueError("edge endpoints must be ULIDs (26 chars)")
        return v


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


class Project(BaseModel):
    id: str = Field(default_factory=new_id)
    slug: str
    name: str
    profile_md: str = ""
    created_at: str = Field(default_factory=now_iso)
    last_active_at: str = Field(default_factory=now_iso)
    config: dict[str, object] = Field(default_factory=dict)

    @field_validator("slug")
    @classmethod
    def _slug_shape(cls, v: str) -> str:
        v = v.strip().lower()
        if not v or not all(c.isalnum() or c in "-_" for c in v):
            raise ValueError("slug must be lowercase alphanumeric / dashes / underscores")
        return v


class ProjectMembership(BaseModel):
    project_id: str
    node_id: str
    role: Role = "included"
    added_at: str = Field(default_factory=now_iso)
    added_by: str = "user"
