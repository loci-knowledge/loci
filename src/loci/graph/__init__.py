"""Graph store: nodes, edges, projects, workspaces, memberships.

The repositories here are the only place that should write to `nodes`,
`raw_nodes`, `interpretation_nodes`, `edges`, `projects`,
`project_membership`, `information_workspaces`, `workspace_sources`,
`workspace_membership`, `project_workspaces`, and `node_tags`. Other layers
(ingest, retrieve, jobs) call into these APIs.

DAG topology (cites: interp→raw, derives_from: interp→interp; both directed,
no cycles) is enforced in `EdgeRepository.create()` because SQL CHECK
constraints can't reach across tables to validate src/dst kind.
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
    "WorkspaceRepository",
]
