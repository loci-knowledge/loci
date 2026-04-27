"""Pre-rendered markdown narrative of a retrieval trace.

The retrieval pipeline returns `trace_table` (per-raw interp paths by id) and
`routing_loci` (the loci that did the routing). Both are data-rich but neither
*tells the story* of how a query reached a raw — the caller has to cross-walk
the two structures by id to see anything.

This module renders that story as a markdown bullet block:

    - **Title of raw** _(score 0.84)_
        ← cites ← [decision] Locus A _(routing 0.62)_
    - **Another raw** _(score 0.62)_
        ← cites ← [philosophy] Locus B _(routing 0.40)_
            ← derives_from ← [tension] Locus C

It surfaces, for each returned raw, *which* locus pointed at it, on *which*
edge, and (where applicable) the upstream locus it derived its routing from.
Direct hits — raws that matched the query without any locus — are flagged
inline so the user can tell when the interpretation layer didn't contribute.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loci.retrieve.pipeline import RetrievedNode, RoutingInterp


def render_trace_narrative(
    *,
    nodes: "list[RetrievedNode]",
    routing_interps: "list[RoutingInterp]",
    max_raws: int = 10,
) -> str:
    """Render a per-raw markdown narrative of how each raw was reached.

    `max_raws` caps how many raws appear in the narrative — beyond that, a
    "…and N more" footer is appended so the caller still knows the ranking
    was longer.
    """
    if not nodes:
        return "_(no results)_"
    interp_by_id = {ri.node_id: ri for ri in routing_interps}
    lines: list[str] = []
    for n in nodes[:max_raws]:
        title = n.title or n.node_id[:12]
        lines.append(f"- **{title}** _(score {n.score:.2f})_")
        if not n.trace:
            why = n.why or "matched the query directly"
            lines.append(f"    direct hit — {why}")
            continue
        for hop in n.trace:
            src_label = _locus_label(hop.src, interp_by_id)
            if hop.edge_type == "cites":
                lines.append(
                    f"    ← cites ← {src_label} "
                    f"_(routing {hop.interp_score:.2f})_"
                )
            else:  # derives_from: src → dst (both loci)
                dst_label = _locus_label(hop.dst, interp_by_id)
                lines.append(
                    f"    ← derives_from ← {src_label} → {dst_label}"
                )
    if len(nodes) > max_raws:
        lines.append(f"_…and {len(nodes) - max_raws} more raws._")
    return "\n".join(lines)


def _locus_label(node_id: str, interp_by_id: "dict[str, RoutingInterp]") -> str:
    ri = interp_by_id.get(node_id)
    if ri is None:
        return f"locus `{node_id[:8]}…`"
    return f"[{ri.subkind}] {ri.title}"
