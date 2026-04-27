# Graph Web UI

loci ships a hosted D3 force-directed graph UI at `/graph/{project_id}`. It
replaces the static D3 export (`loci graph export`) with a live, interactive
interface that updates in real time as you retrieve, draft, and reflect.

## Launching

```bash
uv run loci server          # must be running first
uv run loci graph serve     # prints the URL, e.g. http://127.0.0.1:7077/graph/<id>
```

The server binds to `127.0.0.1` only — not exposed publicly without an auth
middleware in front.

## Layout

```
┌──────────────────────────────────────────────────────────────────┐
│  Sidebar (310px)              │   Graph canvas                   │
│  ┌─────────────────────────┐  │                                   │
│  │  loci   ●  N nodes      │  │   (D3 force-directed graph)      │
│  ├─────────────────────────┤  │                                   │
│  │  [Chat history — queries│  │   Interpretation nodes (colored)  │
│  │   and trace responses]  │  │   Raw nodes (gray dots)           │
│  │                         │  │   Edges (cites / derives_from)    │
│  ├─────────────────────────┤  │                                   │
│  │  [retrieve] [draft]     │  │   Legend (bottom-right)           │
│  │  [Query input] [Send]   │  │                                   │
│  └─────────────────────────┘  │   [Node panel slides in on click] │
└──────────────────────────────────────────────────────────────────┘
```

### Left sidebar

The sidebar is a chat-style interface. Each query you send appears as a user
bubble; the system response expands below it as a collapsible trace card.

- **Mode toggle** — switch between `RETRIEVE` (semantic search) and `DRAFT`
  (cited markdown generation) before sending.
- **Trace card** — collapsible panel showing which routing loci were activated
  and which sources they pointed at. Click any item to highlight the path on
  the graph and zoom to it.

### Graph canvas

The canvas renders a D3 v7 force simulation with link, charge, center, and
collision forces. You can:

- **Pan and zoom** — scroll or pinch to zoom; drag the background to pan.
- **Drag nodes** — pin a node's position by dragging it (simulation stops for
  that node; resume by double-clicking).
- **Click a node** — opens the node detail panel on the right edge.
- **Hover a node** — shows a tooltip with title, subkind, angle (if set), and
  confidence.

### Node detail panel

Slides in from the right edge on node click. Three tabs:

| tab | content |
|-----|---------|
| **Overview** | `relation_md`, `overlap_md`, `source_anchor_md`, angle badge, confidence |
| **Edit** | textarea pre-filled with the three locus slots; `PATCH /nodes/:id/locus` on save |
| **Preview** | rendered markdown preview of the edited locus |

Saving an edit writes `X-Loci-Actor: user` on the request, which is recorded
in the `node_revisions` table for full history tracking.

## Node encoding

### Colors

Interpretation nodes are colored by their `angle` value. The UI hashes the
angle string to one of 8 palette families:

| family | fill | stroke | label |
|--------|------|--------|-------|
| analysis   | dark navy  | blue   | `#7db8ff` |
| evidence   | dark green | green  | `#68e88a` |
| theory     | dark purple | violet | `#c484ff` |
| problem    | dark rust  | orange | `#fdba74` |
| connection | dark teal  | teal   | `#5eead4` |
| framework  | deep indigo | indigo | `#a5b4fc` |
| critical   | dark rose  | rose   | `#fb7185` |
| insight    | dark lime  | lime   | `#bef264` |

Nodes with no `angle` (philosophy, tension, decision subkinds) map to the
`analysis` family.

**Raw nodes** are always gray (`stroke: #5a8ab0`) regardless of content.

### Size

- **Raw nodes**: radius scales with `access_count` — frequently retrieved
  sources appear larger (`r = clamp(5–11, 5 + √access_count × 0.6)`).
- **Interpretation nodes**: radius combines content density (sum of locus slot
  lengths), `access_count`, and degree (number of edges). Range 10–26px.

### Labels

Short labels (first 24 chars of title) are shown on interpretation nodes
only. Label opacity fades in as you zoom: fully hidden at zoom < 0.28, fully
visible at zoom > 0.50. This keeps the canvas uncluttered at the overview
level. Raw nodes show their title only in the hover tooltip.

## Edge encoding

| edge type | style |
|-----------|-------|
| `cites` (interp → raw) | thin, semi-transparent |
| `derives_from` (interp → interp) | thicker, lighter |

During a retrieve or draft trace, edges within the active path turn bright and
animate a pulse from routing locus → source node. Non-involved nodes fade to
7% opacity.

## Trace-of-thought visualization

This is the core knowledge-graph-driven RAG interaction. When you send a
query, the graph **shows you which loci the retrieval pipeline visited** — not
just a flat list of results.

### Retrieve mode

After a retrieve call the graph:

1. Highlights **routing loci** with a gold glow — these are the interpretation
   nodes the pipeline scored highest for your query.
2. Highlights the **raw sources** they point at with a mint glow.
3. Draws bright animated edges from each routing locus to its sources.
4. Fades all other nodes to near-invisible.

In the sidebar, the trace card shows a hierarchical "thought path":

```
Routing locus → [angle] "title of the locus"
  ├─ [pdf] source title (score 0.82)
  └─ [md]  another source (score 0.74)
```

Each line is clickable — clicking zooms the graph and highlights that node.

### Draft mode

After a draft call the canvas shows the same routing locus → source
highlighting as retrieve, but the sidebar also renders the full markdown
output with inline citations.

**Citations are clickable**: `[C1]` markers are rendered as `<cite>` elements.
Clicking a citation zooms the graph to that raw node and highlights the
routing loci that pointed at it. Each citation carries a **verdict badge**:

| badge | meaning |
|-------|---------|
| `supported` | entailment verifier confirmed the claim is grounded |
| `partial` | claim partially grounded |
| `unsupported` | verifier found the citation does not support the claim |
| `unknown` | verifier did not run for this chunk |

The routing loci shown in the trace card correspond to `citation.routed_by[]`
in the API response — the loci that actually caused each raw to be promoted
into the draft's candidate block.

## Live updates (WebSocket)

The canvas subscribes to `WS /projects/:id/subscribe`. When the background
reflect cycle creates or modifies a node or edge, a delta frame arrives and
the graph re-renders incrementally. New nodes appear with a brief entrance
animation. The `●` dot in the sidebar header turns green while the socket is
connected.

Frame shapes the reducer handles:

| shape | effect |
|-------|--------|
| `node_upsert` | insert or update node in simulation |
| `node_delete` | remove node and its incident links |
| `edge_upsert` | insert or update link |
| `edge_delete` | remove link |
| `trace_run` | trigger trace-of-thought highlight for a retrieve/draft |

## Legend

The legend (bottom-right corner) is generated at runtime from the `angle`
values actually present in the graph. It shows only the palette families that
appear in the current project's interpretation nodes, plus one entry for raw
sources. Hover a legend row to temporarily dim all nodes that don't match that
family.

## Keyboard shortcuts

| key | action |
|-----|--------|
| `/` | focus the query input |
| `Esc` | close node panel / clear trace highlight |
| `Tab` | toggle retrieve / draft mode |

## API routes backing the UI

| route | used for |
|-------|----------|
| `GET /projects/:id/graph` | initial graph load (nodes + edges) |
| `GET /projects/:id/retrieve` | retrieve mode queries |
| `POST /projects/:id/draft` | draft mode queries |
| `GET /nodes/:id/revisions` | revision history in node panel |
| `PATCH /nodes/:id/locus` | save edits from node panel |
| `WS /projects/:id/subscribe` | live graph updates |
| `POST /projects/:id/mcp/publish-trace` | MCP trace bridge |

## Comparison with the VSCode extension

| feature | Graph Web UI | Loki Town (VSCode) |
|---------|-------------|-------------------|
| runtime dependency | browser only | VSCode + build step |
| graph rendering | D3 force-directed | PixiJS sprite town |
| retrieve/draft | left sidebar | draft input at bottom |
| trace-of-thought | highlighted graph path | villager walk animation |
| live updates | WebSocket delta | WebSocket delta |
| node editing | inline edit tab | detail pane |
| citation clickability | yes, zooms to node | no |
| verdict badges on citations | yes | no |

Both clients consume the same REST + WebSocket API. Run both at the same time
if you want — they stay in sync through the WS bus.
