"""
ConceptNet 5.5 edge-type taxonomy, borrowed as a controlled vocabulary.

No ConceptNet API or bulk download required — we use these as semantic labels
for concept_edges.relation_hint and aspect_vocab.conceptnet_relation_hint.

Reference: Speer et al. 2017, https://arxiv.org/abs/1612.03975

The structural edge types (cites, wikilink, co_aspect, co_folder, custom) are
loci-specific and separate from the ConceptNet relation hints. Both live under
the same `edge_type` / `relation_hint` column namespace in concept_edges.
"""

CONCEPTNET_EDGE_TYPES: dict[str, str] = {
    "IsA":            "X is a kind of Y",
    "UsedFor":        "X is used for Y",
    "HasProperty":    "X has property Y",
    "PartOf":         "X is a part of Y",
    "RelatedTo":      "X is broadly related to Y",
    "Causes":         "X causes Y",
    "HasContext":     "X appears in the context of Y",
    "SimilarTo":      "X is similar to Y",
    "Entails":        "X entails or implies Y",
    "DefinedAs":      "X is defined as Y",
    "CapableOf":      "X is capable of Y",
    "ReceivesAction": "Y can be done to X",
}

# Structural edge types native to loci (not ConceptNet relation hints).
STRUCTURAL_EDGE_TYPES: list[str] = [
    "cites",       # resource A cites / links to resource B
    "wikilink",    # [[wikilink]] from one note to another
    "co_aspect",   # two resources share at least one aspect tag
    "co_folder",   # two resources live in the same folder
    "custom",      # caller-defined edge with no standard type
]


def valid_edge_types() -> list[str]:
    """All valid values for concept_edges.edge_type (structural + relation hints)."""
    return STRUCTURAL_EDGE_TYPES + list(CONCEPTNET_EDGE_TYPES.keys())


def describe(relation_hint: str) -> str | None:
    """Human-readable description for a relation hint, or None if not in vocab."""
    return CONCEPTNET_EDGE_TYPES.get(relation_hint)


def is_structural(edge_type: str) -> bool:
    """Return True if the edge_type is a structural loci type (not a ConceptNet hint)."""
    return edge_type in STRUCTURAL_EDGE_TYPES


def is_conceptnet(edge_type: str) -> bool:
    """Return True if the edge_type is a ConceptNet relation hint."""
    return edge_type in CONCEPTNET_EDGE_TYPES
