"""Graph store: nodes, edges, projects, workspaces, memberships.

PLAN.md §Data model is the spec. The repositories here are the only place that
should write to `nodes`, `raw_nodes`, `interpretation_nodes`, `edges`,
`projects`, `project_membership`, `information_workspaces`, `workspace_sources`,
`workspace_membership`, `project_workspaces`, and `node_tags`. Other layers
(ingest, retrieve, jobs) call into these APIs.

Edge symmetry / inverse maintenance is enforced here. The schema can't express
"inserting a symmetric edge implies its reciprocal exists" via SQL constraints,
so the rule lives in `EdgeRepository.create()`.
"""

from loci.graph.edges import EdgeRepository
from loci.graph.models import (
    Edge,
    EdgeType,
    InterpretationNode,
    InterpretationOrigin,
    InterpretationSubkind,
    Node,
    NodeKind,
    NodeStatus,
    Project,
    ProjectMembership,
    ProjectWorkspace,
    RawNode,
    RawSubkind,
    RelevanceAngle,
    Role,
    Workspace,
    WorkspaceKind,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceSource,
    new_id,
)
from loci.graph.nodes import NodeRepository
from loci.graph.projects import ProjectRepository
from loci.graph.sources import ProjectSource, SourceRepository
from loci.graph.workspaces import WorkspaceRepository

__all__ = [
    # Nodes
    "Edge",
    "EdgeType",
    "InterpretationNode",
    "InterpretationOrigin",
    "InterpretationSubkind",
    "Node",
    "NodeKind",
    "NodeStatus",
    "RawNode",
    "RawSubkind",
    "new_id",
    # Projects
    "Project",
    "ProjectMembership",
    "Role",
    # Workspaces
    "ProjectWorkspace",
    "RelevanceAngle",
    "Workspace",
    "WorkspaceKind",
    "WorkspaceMembership",
    "WorkspaceRole",
    "WorkspaceSource",
    # Repositories
    "EdgeRepository",
    "NodeRepository",
    "ProjectRepository",
    "ProjectSource",
    "SourceRepository",
    "WorkspaceRepository",
]
