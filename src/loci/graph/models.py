"""Graph models.

These are the data shapes used by the graph layer repositories and the API
layer. They mirror the SQL schema. All writes go through the repository classes;
models here are the in-Python representation returned from and passed to repos.

RawSubkind keeps the existing coarse file-type taxonomy.
Aspect / ConceptEdge / ResourceProvenance are the new concept-layer models that
replace the old interpretation-node / locus DAG.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

import ulid

# ---------------------------------------------------------------------------
# Enums / Literal aliases — keep in sync with SQL CHECK constraints
# ---------------------------------------------------------------------------

RawSubkind = Literal["pdf", "md", "code", "html", "transcript", "txt", "image"]

# Workspace kind — coarse hint for retrieval weighting and prompt phrasing.
WorkspaceKind = Literal["papers", "codebase", "notes", "transcripts", "web", "mixed"]

# Role of a node within a project membership.
Role = Literal["included", "excluded", "pinned"]

# Role of a workspace within a project link.
WorkspaceRole = Literal["primary", "reference", "excluded"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def new_id() -> str:
    """Generate a fresh ULID. 26 chars, time-sortable, base32."""
    return str(ulid.new())


def now_iso() -> str:
    """ISO 8601 UTC timestamp matching SQLite's strftime('%Y-%m-%dT%H:%M:%fZ', 'now')."""
    dt = datetime.now(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# Raw source nodes
# ---------------------------------------------------------------------------


@dataclass
class Node:
    """Base node shape — shared columns for all node types."""
    id: str = field(default_factory=new_id)
    kind: str = ""     # "raw" for now; extensible
    subkind: str = ""
    title: str = ""
    body: str = ""
    tags: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    last_accessed_at: str | None = None
    access_count: int = 0
    confidence: float = 1.0
    status: str = "live"


@dataclass
class RawNode(Node):
    """A raw source file (PDF, Markdown, code, …).

    These are the leaves of the graph — actual user content. They are never
    authored here; they are produced by the ingest pipeline.
    """
    kind: str = "raw"
    subkind: str = "txt"          # overridden to RawSubkind at ingest time
    content_hash: str = ""
    canonical_path: str = ""
    mime: str = ""
    size_bytes: int = 0
    source_of_truth: bool = True
    folder: str | None = None     # logical folder label, e.g. the workspace root subdir


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


@dataclass
class Project:
    id: str = field(default_factory=new_id)
    slug: str = ""
    name: str = ""
    profile_md: str = ""
    created_at: str = field(default_factory=now_iso)
    last_active_at: str = field(default_factory=now_iso)
    config: dict = field(default_factory=dict)


@dataclass
class ProjectMembership:
    project_id: str = ""
    node_id: str = ""
    role: str = "included"        # Role
    added_at: str = field(default_factory=now_iso)
    added_by: str = "user"


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------


@dataclass
class Workspace:
    id: str = field(default_factory=new_id)
    slug: str = ""
    name: str = ""
    description_md: str = ""
    kind: str = "mixed"           # WorkspaceKind
    created_at: str = field(default_factory=now_iso)
    last_active_at: str = field(default_factory=now_iso)
    last_scanned_at: str | None = None
    config: dict = field(default_factory=dict)


@dataclass
class WorkspaceSource:
    id: str = field(default_factory=new_id)
    workspace_id: str = ""
    root_path: str = ""
    label: str | None = None
    added_at: str = field(default_factory=now_iso)
    last_scanned_at: str | None = None


@dataclass
class WorkspaceMembership:
    workspace_id: str
    node_id: str
    added_at: str = field(default_factory=now_iso)


@dataclass
class ProjectWorkspace:
    project_id: str
    workspace_id: str
    linked_at: str = field(default_factory=now_iso)
    role: str = "reference"       # WorkspaceRole
    weight: float = 1.0
    last_relevance_pass_at: str | None = None


# ---------------------------------------------------------------------------
# Concept layer — Aspects, ConceptEdges, ResourceProvenance
# ---------------------------------------------------------------------------


@dataclass
class Aspect:
    """A named concept or tag in the aspect vocabulary.

    Aspects are the unit of semantic grouping across resources. They can be
    user-defined (hand-crafted labels), auto-inferred (produced by the ingest
    pipeline from content), or folder-derived (inherited from a workspace
    folder path).

    `conceptnet_relation_hint` is an optional ConceptNet 5.5 relation label
    (IsA, UsedFor, PartOf, …) that describes how resources tagged with this
    aspect relate to the concept. See `conceptnet_types.py`.
    """
    id: str = field(default_factory=new_id)
    label: str = ""
    description: str | None = None
    conceptnet_relation_hint: str | None = None   # IsA, UsedFor, PartOf, etc.
    user_defined: bool = False
    auto_inferred: bool = False
    last_used: str | None = None
    created_at: str = field(default_factory=now_iso)


@dataclass
class ResourceAspect:
    """Association between a resource (raw node) and an aspect.

    `source` records how the tag arrived: typed by the user ("user"), derived
    from a folder path ("folder"), produced by the ingest model ("inferred"),
    or promoted by repeated retrieval access ("usage").
    """
    resource_id: str
    aspect_id: str
    confidence: float = 1.0
    source: str = "user"    # user | folder | inferred | usage
    created_at: str = field(default_factory=now_iso)


@dataclass
class ConceptEdge:
    """A typed directed edge between two resources in the concept graph.

    `edge_type` is a structural type (cites, wikilink, co_aspect, co_folder,
    custom) or a ConceptNet relation hint (IsA, UsedFor, …).  The distinction
    lets callers filter graph traversal to just structural links or to semantic
    links independently.

    `metadata` is an optional JSON-serialisable dict for caller-specific data
    (e.g. the anchor text for a wikilink, or the chunk IDs for a cites edge).
    """
    id: str = field(default_factory=new_id)
    src_id: str = ""
    dst_id: str = ""
    edge_type: str = "custom"
    relation_hint: str | None = None
    weight: float = 1.0
    metadata: dict | None = None
    created_at: str = field(default_factory=now_iso)


@dataclass
class ResourceProvenance:
    """Provenance metadata for a raw resource.

    Tracks how and where a resource entered the graph: which URL it came from,
    which folder it lives in, what tool ingested it, and optional context text
    (e.g. the surrounding paragraph from a web page, or a user's note).
    """
    resource_id: str
    source_url: str | None = None
    folder: str | None = None
    saved_via: str = "cli"        # cli | mcp | watch
    context_text: str | None = None
    captured_at: str = field(default_factory=now_iso)
