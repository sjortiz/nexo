"""The knowledge graph as a self-contained interactive HTML page, or a live one.

    nexo visualize [--out graph.html] [--open]      (or: make visualize OPEN=1)
    nexo visualize --live [--port 8767] [--interval 5] [--open]   (make visualize LIVE=1)

Live mode serves the page on loopback and re-reads the graph every few seconds,
applying only what changed, so you can watch an ingest fill it in.

Reads Neo4j with the configured settings (the read-only user when one is set)
and renders it with vis-network: documents are dark boxes; entities are dots
coloured by type and sized by how many relations they have; an entity mentioned
in two or more documents gets a thick border (it bridges documents); dashed
lines link entities to the documents that mention them. Hover an edge for its
evidence quote and an entity for its attributes.

The page embeds the graph data, so treat it like the documents it came from;
only the vis-network library is fetched from cdnjs.
"""

from __future__ import annotations

import html
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from nexo.config import get_settings
from nexo.storage.graph_store import GraphStore

LEXICAL = ["MENTIONED_IN", "PART_OF", "NEXT_CHUNK", "FIRST_CHUNK"]
PALETTE = [
    "#4e79a7",
    "#f28e2b",
    "#59a14f",
    "#e15759",
    "#b07aa1",
    "#76b7b2",
    "#edc948",
    "#9c755f",
    "#bab0ac",
]
VIS_NETWORK = (
    "https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.9/standalone/umd/"
    "vis-network.min.js"
)

_DOCUMENTS = "MATCH (d:Document) RETURN d.source_id AS id, d.title AS title"
_ENTITIES = """
MATCH (e:Entity)-[:MENTIONED_IN]->(:Chunk)-[:PART_OF]->(d:Document)
WITH e, collect(DISTINCT d.source_id) AS docs
OPTIONAL MATCH (e)-[m:MENTIONED_IN]->() WHERE m.attributes_json IS NOT NULL
RETURN e.key AS key, e.name AS name, e.type AS type, docs,
       collect(m.attributes_json) AS attrs,
       COUNT { (e)-[r]-(:Entity) WHERE NOT type(r) IN $lexical } AS degree
"""
_RELATIONS = """
MATCH (a:Entity)-[r]->(b:Entity) WHERE NOT type(r) IN $lexical
RETURN a.key AS head, b.key AS tail, type(r) AS relation, r.evidence AS evidence
"""


def read_graph() -> dict[str, list[dict[str, Any]]]:
    """Documents, entities (with their documents and attributes) and relations."""
    store = GraphStore.from_settings(get_settings(), read_only=True)
    try:
        with store.session(write=False) as session:
            return {
                "documents": session.run(_DOCUMENTS).data(),
                "entities": session.run(_ENTITIES, lexical=LEXICAL).data(),
                "relations": session.run(_RELATIONS, lexical=LEXICAL).data(),
            }
    finally:
        store.close()


def type_colors(graph: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
    """A colour per entity type in the graph (built-in or a deployment's own)."""
    types = sorted({e["type"] for e in graph["entities"]})
    return {t: PALETTE[i % len(PALETTE)] for i, t in enumerate(types)}


def to_network(graph: dict[str, list[dict[str, Any]]]) -> tuple[list[dict], list[dict]]:
    """vis-network nodes and edges for the graph."""
    nodes: list[dict] = []
    edges: list[dict] = []
    colors = type_colors(graph)
    for doc in graph["documents"]:
        nodes.append(
            {
                "id": "doc:" + doc["id"],
                "label": doc["title"] or doc["id"],
                "shape": "box",
                "color": "#222",
                "font": {"color": "#fff", "size": 18},
                "mass": 3,
                "title": "Document",
            }
        )
    for entity in graph["entities"]:
        color = colors[entity["type"]]
        bridge = len(entity["docs"]) > 1
        attributes = [a for raw in entity["attrs"] for a in json.loads(raw)]
        tooltip = "\n".join(
            [
                f"{entity['type']}: {entity['name']}",
                f"in {len(entity['docs'])} document(s)",
            ]
            + [f"{a['name']} = {a['value']}" for a in attributes]
        )
        nodes.append(
            {
                "id": entity["key"],
                "label": entity["name"],
                "title": tooltip,
                "color": {"background": color, "border": "#000" if bridge else color},
                "borderWidth": 4 if bridge else 1,
                "value": 1 + entity["degree"],
            }
        )
        for doc_id in entity["docs"]:
            edges.append(
                {
                    "from": entity["key"],
                    "to": "doc:" + doc_id,
                    "dashes": True,
                    "color": {"color": "#ccc"},
                    "width": 0.5,
                }
            )
    for relation in graph["relations"]:
        edges.append(
            {
                "from": relation["head"],
                "to": relation["tail"],
                "label": relation["relation"],
                "arrows": "to",
                "title": relation["evidence"] or "",
                "font": {"size": 9, "align": "middle"},
            }
        )
    return nodes, edges


def summary(graph: dict[str, list[dict[str, Any]]]) -> str:
    """'N documents, N entities, N relations'."""
    return (
        f"{len(graph['documents'])} documents, {len(graph['entities'])} entities, "
        f"{len(graph['relations'])} relations"
    )


def live_payload(graph: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """What a live page polls: the network and its summary."""
    nodes, edges = to_network(graph)
    seen: dict[str, int] = {}
    for edge in edges:  # stable ids, so an update changes edges instead of adding them
        base = f"{edge['from']}|{edge.get('label', 'mention')}|{edge['to']}"
        seen[base] = seen.get(base, 0) + 1  # RELATED_TO can repeat per predicate
        edge["id"] = base if seen[base] == 1 else f"{base}|{seen[base]}"
    return {"nodes": nodes, "edges": edges, "summary": summary(graph)}


# Polls graph.json and applies the difference, so the layout does not restart.
_LIVE_SCRIPT = """
setInterval(async () => {{
  try {{
    const fresh = await (await fetch("graph.json", {{cache: "no-store"}})).json();
    for (const [set, rows] of [[nodes, fresh.nodes], [edges, fresh.edges]]) {{
      const keep = new Set(rows.map(r => r.id));
      set.remove(set.getIds().filter(id => !keep.has(id)));
      set.update(rows);
    }}
    document.getElementById("summary").textContent = fresh.summary;
    document.getElementById("updated").textContent = new Date().toLocaleTimeString();
  }} catch (e) {{
    document.getElementById("updated").textContent = "update failed: " + e;
  }}
}}, {interval_ms});
"""


def render(
    graph: dict[str, list[dict[str, Any]]], *, live_interval_s: float | None = None
) -> str:
    """The HTML page; with `live_interval_s`, it re-reads graph.json at that interval."""
    nodes, edges = to_network(graph)
    if live_interval_s:
        payload_data = live_payload(graph)
        nodes, edges = payload_data["nodes"], payload_data["edges"]
    legend = " ".join(
        f'<span style="background:{color};color:#fff;padding:2px 6px;'
        f'border-radius:3px">{html.escape(name)}</span>'
        for name, color in type_colors(graph).items()
    )
    live = (
        _LIVE_SCRIPT.format(interval_ms=int(live_interval_s * 1000))
        if live_interval_s
        else ""
    )
    updated = ' Live, updated <span id="updated">now</span>.' if live_interval_s else ""
    # json.dumps output is safe inside <script> once "</" cannot close the tag.
    data = {"nodes": nodes, "edges": edges}
    payload = json.dumps(data).replace("</", "<\\/")
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Nexo graph</title>
<script src="{VIS_NETWORK}"></script>
<style>
body {{ margin: 0; font: 13px system-ui; background: #fafafa; }}
header {{ padding: 8px 12px; border-bottom: 1px solid #ddd; }}
#graph {{ height: 90vh; }}
</style></head>
<body>
<header><b>Nexo graph</b>: <span id="summary">{summary(graph)}</span>.{updated}
Dark boxes are documents; a thick border marks an entity found in 2+ documents; dashed lines are mentions. Hover for evidence and attributes.
<br>{legend}</header>
<div id="graph"></div>
<script>
const data = {payload};
const nodes = new vis.DataSet(data.nodes), edges = new vis.DataSet(data.edges);
new vis.Network(document.getElementById("graph"), {{ nodes, edges }}, {{
  nodes: {{ shape: "dot", scaling: {{ min: 8, max: 40 }}, font: {{ size: 12 }} }},
  physics: {{ solver: "forceAtlas2Based", stabilization: {{ iterations: 300 }} }},
  interaction: {{ hover: true, tooltipDelay: 100 }}
}});
{live}</script>
</body></html>
"""


def export(out: Path, *, open_browser: bool = False) -> dict[str, int]:
    """Write the page to `out` (optionally open it); return what it contains."""
    graph = read_graph()
    out.write_text(render(graph), encoding="utf-8")
    if open_browser:
        webbrowser.open(out.resolve().as_uri())
    return {name: len(rows) for name, rows in graph.items()}


class _LiveHandler(BaseHTTPRequestHandler):
    """GET / is the page, GET /graph.json the current network; nothing else."""

    interval_s: float = 5.0

    def do_GET(self) -> None:  # pylint: disable=invalid-name  # http.server API
        """Serve the page or the polled data, read from the graph on each request."""
        path = self.path.split("?", 1)[0]
        if path == "/":
            body = render(read_graph(), live_interval_s=self.interval_s).encode()
            kind = "text/html; charset=utf-8"
        elif path == "/graph.json":
            body = json.dumps(live_payload(read_graph())).encode()
            kind = "application/json"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # keep stdout and stderr quiet
        """Polling would otherwise log a line every few seconds."""


def live_server(host: str, port: int, interval_s: float) -> ThreadingHTTPServer:
    """A server for the live page (loopback only: the caller checks the host)."""
    handler = type("LiveHandler", (_LiveHandler,), {"interval_s": interval_s})
    return ThreadingHTTPServer((host, port), handler)
