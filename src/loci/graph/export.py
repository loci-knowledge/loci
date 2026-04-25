"""Render a standalone HTML snapshot of a project graph.

The graph data comes from the same SQL shape used by the REST endpoint, but
this module turns it into a self-contained D3 visualization that can be saved
to disk and opened directly in a browser.
"""

from __future__ import annotations

import html
import json
import sqlite3
from pathlib import Path

from loci.api.routes.graph_view import get_graph
from loci.graph.models import Project


def build_graph_payload(
    project: Project,
    conn: sqlite3.Connection,
    *,
    include_raw: bool = True,
    statuses: list[str] | None = None,
) -> dict[str, object]:
    """Return the graph payload plus a few render-friendly extras."""
    graph = get_graph(
        project=project,
        include_raw=include_raw,
        statuses=statuses or ["live", "dirty"],
        conn=conn,
    )
    nodes = list(graph["nodes"])
    edges = [
        {
            "source": edge["src"],
            "target": edge["dst"],
            "type": edge["type"],
            "weight": edge["weight"],
        }
        for edge in graph["edges"]
    ]
    interpretation_count = sum(1 for node in nodes if node["kind"] == "interpretation")
    raw_count = sum(1 for node in nodes if node["kind"] == "raw")
    return {
        "project": {"id": project.id, "slug": project.slug, "name": project.name},
        "nodes": nodes,
        "edges": edges,
        "community_version": graph["community_version"],
        "stats": {
            "total_nodes": len(nodes),
            "interpretation_nodes": interpretation_count,
            "raw_nodes": raw_count,
            "edges": len(edges),
        },
    }


def render_graph_html(payload: dict[str, object]) -> str:
    """Render a standalone HTML document with an embedded D3 force graph."""
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    project_name = html.escape(str(payload["project"]["name"]))  # type: ignore[index]
    return _HTML_TEMPLATE.replace("__PROJECT_NAME__", project_name).replace(
        "__DATA_JSON__", data_json
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


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Loci Graph — __PROJECT_NAME__</title>
  <script src="https://d3js.org/d3.v7.min.js"></script>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0b1020;
      --panel: rgba(16, 24, 40, 0.9);
      --line: #1f2a44;
      --text: #dbe4ff;
      --muted: #8b9bb7;
      --raw: #64748b;
      --question: #93c5fd;
      --relevance: #86efac;
      --tension: #fca5a5;
      --philosophy: #c4b5fd;
      --pattern: #fcd34d;
      --decision: #67e8f9;
      --cites: #6366f1;
      --reinforces: #22c55e;
      --extends: #f59e0b;
      --co_occurs: #1e3a5f;
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; width: 100%; height: 100%; overflow: hidden; background: radial-gradient(circle at top, #101b39 0, var(--bg) 45%); color: var(--text); font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
    #canvas { width: 100vw; height: 100vh; display: block; }
    #chrome, #stats, #panel, #filters, #legend {
      position: fixed; z-index: 2; backdrop-filter: blur(10px);
      border: 1px solid var(--line); background: var(--panel); border-radius: 12px;
      box-shadow: 0 18px 48px rgba(0, 0, 0, 0.32);
    }
    #chrome { top: 16px; left: 16px; padding: 12px 14px; max-width: 520px; }
    #chrome h1 { margin: 0 0 4px; font-size: 18px; }
    #chrome p { margin: 0; color: var(--muted); font-size: 12px; line-height: 1.5; }
    #stats { top: 16px; right: 360px; padding: 10px 12px; font-size: 12px; color: var(--muted); }
    #filters { top: 72px; left: 50%; transform: translateX(-50%); padding: 8px; display: flex; gap: 6px; flex-wrap: wrap; justify-content: center; max-width: min(92vw, 860px); }
    #legend { bottom: 16px; left: 16px; padding: 10px 12px; font-size: 11px; }
    #legend .row { display: flex; align-items: center; gap: 8px; margin: 4px 0; color: var(--muted); }
    .swatch, .line { display: inline-block; }
    .swatch { width: 10px; height: 10px; border-radius: 50%; }
    .line { width: 22px; height: 2px; }
    #panel { top: 16px; right: 16px; width: 320px; height: calc(100vh - 32px); padding: 14px; overflow: auto; display: none; }
    #panel h2 { margin: 0 20px 4px 0; font-size: 16px; line-height: 1.25; }
    #panel .meta { color: var(--muted); font-size: 11px; margin-bottom: 10px; }
    #panel .body { color: #c2cfe8; font-size: 12px; line-height: 1.6; white-space: pre-wrap; }
    #panel .section { margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--line); }
    #panel .label { color: var(--muted); font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; margin-bottom: 6px; }
    #panel-close { position: absolute; top: 10px; right: 10px; cursor: pointer; color: var(--muted); }
    #panel-close:hover { color: var(--text); }
    .btn {
      border: 1px solid transparent; border-radius: 999px; padding: 5px 10px;
      background: rgba(255, 255, 255, 0.04); color: var(--muted);
      font-size: 11px; cursor: pointer;
    }
    .btn.active { color: var(--text); border-color: #30507f; background: rgba(49, 77, 120, 0.4); }
    .node circle { stroke: rgba(255,255,255,0.18); stroke-width: 1.5; }
    .node text { font-size: 10px; fill: #a5b4d4; pointer-events: none; text-anchor: middle; }
    .link { stroke-opacity: 0.45; }
    .link.cites { stroke: var(--cites); }
    .link.reinforces { stroke: var(--reinforces); }
    .link.extends { stroke: var(--extends); stroke-dasharray: 6 3; }
    .link.co_occurs { stroke: var(--co_occurs); stroke-dasharray: 4 3; }
    .link.specializes, .link.generalizes { stroke: #8b5cf6; stroke-dasharray: 4 3; }
  </style>
</head>
<body>
  <div id="chrome">
    <h1>Loci Graph — __PROJECT_NAME__</h1>
    <p>Standalone snapshot generated from the local loci database. Drag nodes, zoom with the mouse wheel, and click a node for details.</p>
  </div>
  <div id="stats"></div>
  <div id="filters"></div>
  <div id="legend">
    <div class="row"><span class="swatch" style="background:var(--question)"></span>question</div>
    <div class="row"><span class="swatch" style="background:var(--relevance)"></span>relevance</div>
    <div class="row"><span class="swatch" style="background:var(--tension)"></span>tension</div>
    <div class="row"><span class="swatch" style="background:var(--philosophy)"></span>philosophy</div>
    <div class="row"><span class="swatch" style="background:var(--pattern)"></span>pattern</div>
    <div class="row"><span class="swatch" style="background:var(--decision)"></span>decision</div>
    <div class="row"><span class="swatch" style="background:var(--raw)"></span>raw</div>
    <div class="row"><span class="line" style="background:var(--cites)"></span>cites</div>
    <div class="row"><span class="line" style="background:var(--reinforces)"></span>reinforces</div>
    <div class="row"><span class="line" style="background:var(--extends)"></span>extends</div>
    <div class="row"><span class="line" style="background:var(--co_occurs)"></span>co_occurs</div>
  </div>
  <div id="panel"><div id="panel-close">✕</div><div id="panel-content"></div></div>
  <svg id="canvas"></svg>
  <script>
    const DATA = __DATA_JSON__;
    const svg = d3.select('#canvas');
    const W = window.innerWidth;
    const H = window.innerHeight;
    svg.attr('width', W).attr('height', H);
    const g = svg.append('g');

    const color = {
      raw: getComputedStyle(document.documentElement).getPropertyValue('--raw').trim(),
      question: getComputedStyle(document.documentElement).getPropertyValue('--question').trim(),
      relevance: getComputedStyle(document.documentElement).getPropertyValue('--relevance').trim(),
      tension: getComputedStyle(document.documentElement).getPropertyValue('--tension').trim(),
      philosophy: getComputedStyle(document.documentElement).getPropertyValue('--philosophy').trim(),
      pattern: getComputedStyle(document.documentElement).getPropertyValue('--pattern').trim(),
      decision: getComputedStyle(document.documentElement).getPropertyValue('--decision').trim(),
    };

    const subkinds = [...new Set(DATA.nodes.filter((node) => node.kind === 'interpretation').map((node) => node.subkind))].sort();
    const active = new Set(subkinds);
    const nodeById = new Map(DATA.nodes.map((node) => [node.id, node]));
    const degree = new Map(DATA.nodes.map((node) => [node.id, 0]));
    for (const edge of DATA.edges) {
      degree.set(edge.source, (degree.get(edge.source) || 0) + 1);
      degree.set(edge.target, (degree.get(edge.target) || 0) + 1);
    }
    for (const node of DATA.nodes) node.degree = degree.get(node.id) || 0;

    const filters = d3.select('#filters');
    const allButton = filters.append('button').attr('class', 'btn active').text('all');
    allButton.on('click', () => {
      subkinds.forEach((subkind) => active.add(subkind));
      updateFilters();
    });
    filters.selectAll('button.subkind')
      .data(subkinds)
      .join('button')
      .attr('class', 'btn active subkind')
      .text((subkind) => subkind)
      .style('color', (subkind) => color[subkind] || 'var(--muted)')
      .on('click', (_, subkind) => {
        if (active.has(subkind)) active.delete(subkind);
        else active.add(subkind);
        updateFilters();
      });

    const links = DATA.edges.map((edge) => ({ ...edge }));
    const nodes = DATA.nodes.map((node) => ({ ...node }));
    const simulation = d3.forceSimulation(nodes)
      .force('link', d3.forceLink(links).id((node) => node.id).distance((edge) => {
        if (edge.type === 'cites') return 120;
        if (edge.type === 'co_occurs') return 72;
        if (edge.type === 'extends') return 96;
        return 80;
      }).strength((edge) => edge.type === 'co_occurs' ? 0.12 : edge.type === 'cites' ? 0.38 : 0.55))
      .force('charge', d3.forceManyBody().strength((node) => node.kind === 'raw' ? -70 : -150))
      .force('center', d3.forceCenter(W / 2, H / 2))
      .force('radial', d3.forceRadial((node) => {
        if (node.kind === 'raw') return 320;
        if (node.subkind === 'question') return 210;
        if (node.subkind === 'relevance') return 180;
        return 110;
      }, W / 2, H / 2).strength(0.45))
      .force('collision', d3.forceCollide((node) => node.kind === 'raw' ? 8 : 13 + Math.min(node.degree * 0.18, 5)));

    const link = g.append('g').selectAll('line')
      .data(links)
      .join('line')
      .attr('class', (edge) => `link ${edge.type}`)
      .attr('stroke-width', (edge) => edge.type === 'cites' ? 1.8 : edge.type === 'reinforces' ? 1.8 : 1.2);

    const node = g.append('g').selectAll('g')
      .data(nodes)
      .join('g')
      .attr('class', 'node')
      .call(d3.drag()
        .on('start', (event, datum) => {
          if (!event.active) simulation.alphaTarget(0.3).restart();
          datum.fx = datum.x;
          datum.fy = datum.y;
        })
        .on('drag', (event, datum) => {
          datum.fx = event.x;
          datum.fy = event.y;
        })
        .on('end', (event, datum) => {
          if (!event.active) simulation.alphaTarget(0);
          datum.fx = null;
          datum.fy = null;
        }))
      .on('click', (event, datum) => {
        event.stopPropagation();
        showPanel(datum);
      });

    node.append('circle')
      .attr('r', (datum) => datum.kind === 'raw' ? 6 : 9 + Math.min(datum.degree * 0.2, 6))
      .attr('fill', (datum) => datum.kind === 'raw' ? color.raw : (color[datum.subkind] || '#94a3b8'));

    node.append('text')
      .attr('dy', (datum) => datum.kind === 'raw' ? 15 : 18)
      .text((datum) => datum.title.length > 24 ? `${datum.title.slice(0, 24)}…` : datum.title);

    simulation.on('tick', () => {
      link
        .attr('x1', (edge) => edge.source.x)
        .attr('y1', (edge) => edge.source.y)
        .attr('x2', (edge) => edge.target.x)
        .attr('y2', (edge) => edge.target.y);
      node.attr('transform', (datum) => `translate(${datum.x},${datum.y})`);
    });

    svg.on('click', () => hidePanel());
    d3.select('#panel-close').on('click', () => hidePanel());
    window.addEventListener('resize', () => location.reload());

    function updateFilters() {
      filters.selectAll('button.subkind').classed('active', (subkind) => active.has(subkind));
      allButton.classed('active', active.size === subkinds.length);
      const visible = new Set(DATA.nodes.filter((datum) => datum.kind === 'raw' || active.has(datum.subkind)).map((datum) => datum.id));
      node.style('opacity', (datum) => visible.has(datum.id) ? 1 : 0.1);
      link.style('opacity', (edge) => visible.has(edge.source.id || edge.source) && visible.has(edge.target.id || edge.target) ? 0.45 : 0.05);
    }

    function showPanel(datum) {
      const panel = document.getElementById('panel');
      const content = document.getElementById('panel-content');
      const role = datum.role ? `<div class="meta">role: ${datum.role}</div>` : '';
      const community = datum.community_id ? `<div class="meta">community: ${datum.community_id}</div>` : '';
      content.innerHTML = `
        <h2>${escapeHtml(datum.title)}</h2>
        <div class="meta">${datum.kind} / ${datum.subkind || 'raw'} · confidence ${Number(datum.confidence).toFixed(2)} · status ${datum.status}</div>
        ${role}${community}
        <div class="body">${escapeHtml(datum.body || '')}</div>
        <div class="section">
          <div class="label">connections</div>
          ${connectionList(datum.id)}
        </div>
      `;
      panel.style.display = 'block';
    }

    function hidePanel() {
      document.getElementById('panel').style.display = 'none';
    }

    function connectionList(id) {
      const rows = DATA.edges.filter((edge) => edge.source === id || edge.target === id).slice(0, 20);
      if (!rows.length) return '<div class="meta">no connections</div>';
      return rows.map((edge) => {
        const otherId = edge.source === id ? edge.target : edge.source;
        const other = nodeById.get(otherId);
        return `<div class="body">${edge.type} → ${escapeHtml(other ? other.title : otherId)}</div>`;
      }).join('');
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    document.getElementById('stats').textContent =
      `${DATA.stats.total_nodes} nodes · ${DATA.stats.interpretation_nodes} interpretation · ${DATA.stats.raw_nodes} raw · ${DATA.stats.edges} edges`;
    updateFilters();
  </script>
</body>
</html>
"""