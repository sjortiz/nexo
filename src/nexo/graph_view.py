"""The knowledge graph as a self-contained interactive HTML page.

    nexo visualize [--out graph.html] [--open]      (or: make visualize OPEN=1)

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


def render(graph: dict[str, list[dict[str, Any]]]) -> str:
    """The HTML page."""
    nodes, edges = to_network(graph)
    legend = " ".join(
        f'<span style="background:{color};color:#fff;padding:2px 6px;'
        f'border-radius:3px">{html.escape(name)}</span>'
        for name, color in type_colors(graph).items()
    )
    summary = (
        f"{len(graph['documents'])} documents, {len(graph['entities'])} entities, "
        f"{len(graph['relations'])} relations"
    )
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
<header><b>Nexo graph</b>: {summary}. Dark boxes are documents; a thick border marks an
entity found in 2+ documents; dashed lines are mentions. Hover for evidence and attributes.
<br>{legend}</header>
<div id="graph"></div>
<script>
const data = {payload};
new vis.Network(document.getElementById("graph"), {{
  nodes: new vis.DataSet(data.nodes), edges: new vis.DataSet(data.edges)
}}, {{
  nodes: {{ shape: "dot", scaling: {{ min: 8, max: 40 }}, font: {{ size: 12 }} }},
  physics: {{ solver: "forceAtlas2Based", stabilization: {{ iterations: 300 }} }},
  interaction: {{ hover: true, tooltipDelay: 100 }}
}});
</script>
</body></html>
"""


def export(out: Path, *, open_browser: bool = False) -> dict[str, int]:
    """Write the page to `out` (optionally open it); return what it contains."""
    graph = read_graph()
    out.write_text(render(graph), encoding="utf-8")
    if open_browser:
        webbrowser.open(out.resolve().as_uri())
    return {name: len(rows) for name, rows in graph.items()}
