"""Graph store: nodes, edges, projects, memberships.

PLAN.md §Data model is the spec. The repositories here are the only place that
should write to `nodes`, `raw_nodes`, `interpretation_nodes`, `edges`,
`projects`, `project_membership`, and `node_tags`. Other layers (ingest,
retrieve, jobs) call into these APIs.

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
    RawNode,
    RawSubkind,
    Role,
    new_id,
)
from loci.graph.nodes import NodeRepository
from loci.graph.projects import ProjectRepository
from loci.graph.sources import ProjectSource, SourceRepository

__all__ = [
    "Edge",
    "EdgeType",
    "InterpretationNode",
    "InterpretationOrigin",
    "InterpretationSubkind",
    "Node",
    "NodeKind",
    "NodeStatus",
    "Project",
    "ProjectMembership",
    "RawNode",
    "RawSubkind",
    "Role",
    "new_id",
    "NodeRepository",
    "EdgeRepository",
    "ProjectRepository",
    "ProjectSource",
    "SourceRepository",
]
