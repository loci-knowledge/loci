"""Render a standalone HTML snapshot of a project graph.

The graph data comes from the same SQL shape used by the REST endpoint, but
this module turns it into a self-contained D3 visualization that can be saved
to disk and opened directly in a browser.

D3 is embedded inline so the file is self-contained with no external deps.
If the cached copy at ~/.loci/d3.v7.min.js is missing, it is fetched once
from the CDN and cached there. Subsequent exports are offline-capable.
"""

from __future__ import annotations

import html
import json
import sqlite3
from pathlib import Path

from loci.api.routes.graph_view import get_graph
from loci.graph.models import Project

_D3_CDN = "https://d3js.org/d3.v7.min.js"


def _d3_inline() -> str:
    """Return the D3 v7 minified source, caching in ~/.loci/d3.v7.min.js."""
    from loci.config import get_settings
    cache_path = get_settings().data_dir / "d3.v7.min.js"
    if cache_path.exists():
        return cache_path.read_text()
    try:
        import httpx
        src = httpx.get(_D3_CDN, timeout=30).text
        cache_path.write_text(src)
        return src
    except Exception:
        # Fall back to CDN script tag; caller will need internet.
        return f'/* d3 inline unavailable — loading from CDN */\ndocument.write(\'<script src="{_D3_CDN}"><\\/script>\')'


def build_graph_payload(
    project: Project,
    conn: sqlite3.Connection,
    *,
    include_raw: bool = True,
    statuses: list[str] | None = None,
) -> dict[str, object]:
    """Return the graph payload plus cited-source lookup and render extras."""
    graph = get_graph(
        project=project,
        include_raw=include_raw,
        statuses=statuses or ["live", "dirty"],
        conn=conn,
    )
    nodes = list(graph["nodes"])
    edges = list(graph["edges"])

    # Build a title lookup for all raw nodes so cited-source names resolve.
    raw_title: dict[str, str] = {
        n["id"]: n["title"] for n in nodes if n["kind"] == "raw"
    }
    # For each interpretation node, collect its cites→raw edges.
    cites_map: dict[str, list[dict]] = {}
    for e in edges:
        if e["type"] == "cites":
            interp_id = e["src"]
            raw_id = e["dst"]
            if raw_id in raw_title:
                cites_map.setdefault(interp_id, []).append(
                    {"id": raw_id, "title": raw_title[raw_id]}
                )

    # Annotate each node with its cited sources.
    for node in nodes:
        node["cited_raws"] = cites_map.get(node["id"], [])

    d3_edges = [
        {
            "source": e["src"],
            "target": e["dst"],
            "type": e["type"],
            "weight": e["weight"],
        }
        for e in edges
    ]

    interpretation_count = sum(1 for n in nodes if n["kind"] == "interpretation")
    raw_count = sum(1 for n in nodes if n["kind"] == "raw")
    return {
        "project": {"id": project.id, "slug": project.slug, "name": project.name},
        "nodes": nodes,
        "edges": d3_edges,
        "community_version": graph["community_version"],
        "stats": {
            "total_nodes": len(nodes),
            "interpretation_nodes": interpretation_count,
            "raw_nodes": raw_count,
            "edges": len(d3_edges),
        },
    }


def render_graph_html(payload: dict[str, object], *, inline_d3: bool = True) -> str:
    """Render a standalone HTML document with an embedded D3 force graph."""
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    project_name = html.escape(str(payload["project"]["name"]))  # type: ignore[index]
    d3_block = f"<script>{_d3_inline()}</script>" if inline_d3 else f'<script src="{_D3_CDN}"></script>'
    return (
        _HTML_TEMPLATE
        .replace("__PROJECT_NAME__", project_name)
        .replace("__DATA_JSON__", data_json)
        .replace("__D3_SCRIPT__", d3_block)
    )


def write_graph_html(
    project: Project,
    conn: sqlite3.Connection,
    output: Path,
    *,
    include_raw: bool = True,
    statuses: list[str] | None = None,
) -> Path:
    """Write a standalone graph HTML file and return the output path."""
    payload = build_graph_payload(
        project,
        conn,
        include_raw=include_raw,
        statuses=statuses,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_graph_html(payload))
    return output


_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Loci Graph — __PROJECT_NAME__</title>
__D3_SCRIPT__
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0f1117; color: #e0e0e0; font-family: monospace; overflow: hidden; display: flex; }
#canvas-wrap { flex: 1; position: relative; }
svg { width: 100%; height: 100vh; }

/* Tooltip */
#tooltip {
  position: fixed; display: none;
  background: rgba(15,17,23,.97); border: 1px solid #2a2d3e;
  border-radius: 6px; padding: 10px 14px; max-width: 320px;
  pointer-events: none; z-index: 100; font-size: 12px; line-height: 1.5;
}
#tooltip .tt-kind  { font-size: 10px; color: #666; margin-bottom: 4px; }
#tooltip .tt-title { font-size: 13px; font-weight: bold; color: #fff; margin-bottom: 6px; }
#tooltip .tt-body  { color: #aaa; margin-bottom: 8px; max-height: 72px; overflow: hidden; }
#tooltip .tt-src-label { font-size: 10px; color: #555; text-transform: uppercase; letter-spacing: .05em; margin-bottom: 4px; }
#tooltip .tt-src   { color: #60a5fa; font-size: 11px; margin-bottom: 2px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
#tooltip .tt-hint  { font-size: 10px; color: #444; margin-top: 6px; }

/* Side panel */
#panel {
  width: 0; overflow: hidden; transition: width .18s ease;
  background: #13151e; border-left: 1px solid #1e2130;
  display: flex; flex-direction: column; position: relative;
}
#panel.open { width: 380px; }
#panel-inner { padding: 20px 20px 40px; overflow-y: auto; flex: 1; }
#panel-close {
  position: absolute; top: 14px; right: 16px;
  background: none; border: none; color: #555; font-size: 18px; cursor: pointer;
}
#panel-close:hover { color: #fff; }
.p-kind  { font-size: 10px; color: #666; text-transform: uppercase;
  letter-spacing: .08em; margin-bottom: 8px; }
.p-title { font-size: 17px; font-weight: bold; color: #fff;
  margin-bottom: 14px; line-height: 1.3; }
.p-section { font-size: 10px; text-transform: uppercase; letter-spacing: .08em;
  color: #444; margin: 16px 0 6px; }
.p-body  { font-size: 12px; color: #bbb; line-height: 1.65; white-space: pre-wrap; }
.p-src   { background: #1a1d28; border: 1px solid #252839;
  border-radius: 4px; padding: 8px 10px; margin-bottom: 6px; }
.p-src-title { font-size: 12px; color: #93c5fd; font-weight: bold; margin-bottom: 2px; }
.p-src-id    { font-size: 10px; color: #3a3d52; }
.p-conn      { font-size: 11px; color: #888; margin-bottom: 3px; }
.p-conn span { color: #bbb; }

/* Legend */
#legend {
  position: fixed; bottom: 16px; left: 16px;
  background: rgba(15,17,23,.92); border: 1px solid #1e2130;
  border-radius: 6px; padding: 10px 14px; font-size: 11px;
}
.lg-row { display: flex; align-items: center; gap: 8px; margin-bottom: 3px; color: #888; }
.lg-dot  { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
.lg-line { width: 20px; height: 2px; flex-shrink: 0; }

/* Stat bar */
#statbar {
  position: fixed; top: 14px; left: 50%; transform: translateX(-50%);
  background: rgba(15,17,23,.9); border: 1px solid #1e2130;
  border-radius: 20px; padding: 5px 14px; font-size: 11px; color: #555;
}
</style>
</head>
<body>
<div id="canvas-wrap">
  <svg id="graph"></svg>
</div>
<div id="panel">
  <button id="panel-close">✕</button>
  <div id="panel-inner">
    <div class="p-kind"  id="p-kind"></div>
    <div class="p-title" id="p-title"></div>
    <div id="p-locus-wrap" style="display:none">
      <div class="p-section">Relation</div>
      <div class="p-body" id="p-relation"></div>
      <div class="p-section">Overlap</div>
      <div class="p-body" id="p-overlap"></div>
      <div class="p-section">Source anchor</div>
      <div class="p-body" id="p-anchor"></div>
      <div id="p-angle-row" class="p-conn" style="display:none">angle: <span id="p-angle"></span></div>
    </div>
    <div id="p-body-wrap" style="display:none">
      <div class="p-section">Body</div>
      <div class="p-body" id="p-body"></div>
    </div>
    <div id="p-sources-wrap" style="display:none">
      <div class="p-section">Cited Sources</div>
      <div id="p-sources"></div>
    </div>
    <div class="p-section">Graph Connections</div>
    <div id="p-conns"></div>
  </div>
</div>
<div id="tooltip">
  <div class="tt-kind"  id="tt-kind"></div>
  <div class="tt-title" id="tt-title"></div>
  <div class="tt-body"  id="tt-body"></div>
  <div id="tt-src-wrap">
    <div class="tt-src-label">Cited Sources</div>
    <div id="tt-src"></div>
  </div>
  <div class="tt-hint">click to expand</div>
</div>
<div id="statbar"></div>
<div id="legend">
  <div style="color:#555;font-size:10px;text-transform:uppercase;letter-spacing:.05em;margin-bottom:5px">Nodes</div>
  <div class="lg-row"><div class="lg-dot" style="background:#ef4444"></div>tension</div>
  <div class="lg-row"><div class="lg-dot" style="background:#eab308"></div>decision</div>
  <div class="lg-row"><div class="lg-dot" style="background:#a855f7"></div>philosophy</div>
  <div class="lg-row"><div class="lg-dot" style="background:#22d3ee"></div>relevance</div>
  <div class="lg-row"><div class="lg-dot" style="background:#6b7280"></div>raw</div>
  <div style="color:#555;font-size:10px;text-transform:uppercase;letter-spacing:.05em;margin:8px 0 4px">Edges (DAG)</div>
  <div class="lg-row"><div class="lg-line" style="background:#4b5563;border-top:2px dashed #4b5563;background:transparent"></div>cites &nbsp;<span style="color:#444">interp→raw</span></div>
  <div class="lg-row"><div class="lg-line" style="background:#a855f7"></div>derives_from &nbsp;<span style="color:#444">interp→interp</span></div>
</div>
<script>
const DATA = __DATA_JSON__;
const NODE_COLOR = {tension:'#ef4444',decision:'#eab308',philosophy:'#a855f7',relevance:'#22d3ee',raw:'#6b7280'};
function nodeColor(d) { return d.kind==='interpretation' ? (NODE_COLOR[d.subkind]||'#888') : NODE_COLOR.raw; }
function nodeRadius(d) {
  if (d.kind==='raw') return 5;
  return {tension:12,decision:14,philosophy:13,relevance:11}[d.subkind] || 10;
}
const nodeById = {};
for (const n of DATA.nodes) nodeById[n.id] = n;

// All edges are directed in the DAG; no symmetric dedup needed.
const links = DATA.edges.map(e=>({...e}));
const nodes = DATA.nodes.map(n=>({...n}));

const W = window.innerWidth, H = window.innerHeight;
const svg = d3.select('#graph').attr('viewBox',[0,0,W,H]);
const g = svg.append('g');
svg.call(d3.zoom().scaleExtent([0.15,8]).on('zoom',ev=>g.attr('transform',ev.transform)));

const sim = d3.forceSimulation(nodes)
  .force('link', d3.forceLink(links).id(d=>d.id)
    .distance(e=>e.type==='derives_from'?100:80).strength(0.45))
  .force('charge', d3.forceManyBody().strength(d=>d.kind==='raw'?-60:-200))
  .force('center', d3.forceCenter(W/2,H/2))
  .force('collide', d3.forceCollide(d=>nodeRadius(d)+5));

// derives_from edges (interp→interp) are solid purple. cites edges (interp→raw)
// are dashed grey, signalling that they exit the locus layer toward a leaf raw.
const link = g.append('g').selectAll('line').data(links).join('line')
  .attr('stroke', e=>e.type==='derives_from'?'#a855f7':'#4b5563')
  .attr('stroke-width', e=>e.type==='cites'?1:1.4)
  .attr('stroke-opacity', e=>e.type==='cites'?0.4:0.7)
  .attr('stroke-dasharray', e=>e.type==='cites'?'4 3':null);

const node = g.append('g').selectAll('circle').data(nodes).join('circle')
  .attr('r', nodeRadius).attr('fill', nodeColor)
  .attr('fill-opacity', d=>d.kind==='raw'?0.55:0.92)
  .attr('stroke', d=>d.kind==='interpretation'?'rgba(255,255,255,.2)':'none')
  .attr('stroke-width', 1.5).style('cursor','pointer')
  .call(d3.drag()
    .on('start',(ev,d)=>{if(!ev.active)sim.alphaTarget(0.3).restart();d.fx=d.x;d.fy=d.y;})
    .on('drag', (ev,d)=>{d.fx=ev.x;d.fy=ev.y;})
    .on('end',  (ev,d)=>{if(!ev.active)sim.alphaTarget(0);d.fx=null;d.fy=null;}));

const label = g.append('g').selectAll('text')
  .data(nodes.filter(d=>d.kind==='interpretation')).join('text')
  .attr('font-size', 9).attr('fill','#ccc').attr('text-anchor','middle')
  .attr('dy', d=>-(nodeRadius(d)+4)).style('pointer-events','none')
  .text(d=>d.title.length>28?d.title.slice(0,26)+'…':d.title);

sim.on('tick',()=>{
  link.attr('x1',e=>e.source.x).attr('y1',e=>e.source.y)
      .attr('x2',e=>e.target.x).attr('y2',e=>e.target.y);
  node.attr('cx',d=>d.x).attr('cy',d=>d.y);
  label.attr('x',d=>d.x).attr('y',d=>d.y);
});

// Stat bar
document.getElementById('statbar').textContent =
  DATA.stats.total_nodes+' nodes · '+DATA.stats.interpretation_nodes+' interp · '+DATA.stats.raw_nodes+' raw · '+DATA.stats.edges+' edges';

// Tooltip
const tooltip=document.getElementById('tooltip');
node.on('mousemove',(ev,d)=>{
  const x=ev.clientX+14, y=ev.clientY-10;
  tooltip.style.display='block';
  tooltip.style.left=Math.min(x,window.innerWidth-340)+'px';
  tooltip.style.top=Math.max(y,8)+'px';
  document.getElementById('tt-kind').textContent='['+d.kind+(d.subkind?':'+d.subkind:'')+']';
  document.getElementById('tt-title').textContent=d.title||'(untitled)';
  const b=(d.body||'').trim();
  const tb=document.getElementById('tt-body');
  tb.textContent=b?b.slice(0,200)+(b.length>200?'…':''):'';
  tb.style.display=b?'block':'none';
  const srcs=d.cited_raws||[];
  const sw=document.getElementById('tt-src-wrap');
  if(srcs.length){
    sw.style.display='block';
    document.getElementById('tt-src').innerHTML=srcs.slice(0,4).map(s=>`<div class="tt-src">↗ ${esc(s.title)}</div>`).join('');
  } else { sw.style.display='none'; }
}).on('mouseleave',()=>{ tooltip.style.display='none'; });

// Panel
const panel=document.getElementById('panel');
node.on('click',(ev,d)=>{
  ev.stopPropagation();
  tooltip.style.display='none';
  const c=nodeColor(d);
  document.getElementById('p-kind').textContent='['+d.kind+(d.subkind?':'+d.subkind:'')+']';
  document.getElementById('p-kind').style.color=c;
  document.getElementById('p-title').textContent=d.title||'(untitled)';

  // Locus slots — only meaningful for interpretation nodes.
  const lw=document.getElementById('p-locus-wrap');
  if (d.kind==='interpretation') {
    lw.style.display='block';
    document.getElementById('p-relation').textContent=(d.relation_md||'').trim()||'—';
    document.getElementById('p-overlap').textContent=(d.overlap_md||'').trim()||'—';
    document.getElementById('p-anchor').textContent=(d.source_anchor_md||'').trim()||'—';
    const ar=document.getElementById('p-angle-row');
    if (d.angle) {
      ar.style.display='block';
      document.getElementById('p-angle').textContent=d.angle;
    } else { ar.style.display='none'; }
  } else { lw.style.display='none'; }

  // Free-form body (for raws and the rare interp body).
  const bw=document.getElementById('p-body-wrap');
  const body=(d.body||'').trim();
  if (body) {
    bw.style.display='block';
    document.getElementById('p-body').textContent=body;
  } else { bw.style.display='none'; }

  const srcs=d.cited_raws||[];
  const sw=document.getElementById('p-sources-wrap');
  if(srcs.length){
    sw.style.display='block';
    document.getElementById('p-sources').innerHTML=srcs.map(s=>`<div class="p-src"><div class="p-src-title">↗ ${esc(s.title)}</div><div class="p-src-id">${s.id}</div></div>`).join('');
  } else { sw.style.display='none'; }
  const conns=links.filter(e=>(e.source.id||e.source)===d.id||(e.target.id||e.target)===d.id).slice(0,20);
  document.getElementById('p-conns').innerHTML=conns.map(e=>{
    const oid=(e.source.id||e.source)===d.id?(e.target.id||e.target):(e.source.id||e.source);
    const o=nodeById[oid];
    return `<div class="p-conn">${e.type} → <span>${esc(o?o.title:oid)}</span></div>`;
  }).join('')||'<div class="p-conn">none</div>';
  panel.classList.add('open');
});
document.getElementById('panel-close').addEventListener('click',()=>panel.classList.remove('open'));
svg.on('click',()=>panel.classList.remove('open'));

function esc(v){ return String(v||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
</script>
</body>
</html>"""