"""`nexo visualize`: the knowledge graph as an interactive HTML page."""

from __future__ import annotations

import json
import re

from nexo import cli
from nexo.graph_view import render, to_network

GRAPH = {
    "documents": [{"id": "a", "title": "Pricing"}, {"id": "b", "title": None}],
    "entities": [
        {
            "key": "Organization:acme holdings",
            "name": "Acme Holdings",
            "type": "Organization",
            "docs": ["a", "b"],
            "attrs": [],
            "degree": 2,
        },
        {
            "key": "Product:starter plan",
            "name": "Starter Plan",
            "type": "Product",
            "docs": ["a"],
            "attrs": [json.dumps([{"name": "price", "value": "$49", "evidence": "e"}])],
            "degree": 1,
        },
    ],
    "relations": [
        {
            "head": "Organization:acme holdings",
            "tail": "Product:starter plan",
            "relation": "OFFERS",
            "evidence": "Acme offers the Starter Plan </script>",
        }
    ],
}


def test_network_marks_bridges_mentions_and_attributes():
    """Entities in 2+ documents get a thick border; mentions are dashed; attributes show."""
    nodes, edges = to_network(GRAPH)
    by_id = {n["id"]: n for n in nodes}
    assert by_id["doc:b"]["label"] == "b"  # untitled documents fall back to their id
    assert by_id["Organization:acme holdings"]["borderWidth"] == 4
    assert by_id["Product:starter plan"]["borderWidth"] == 1
    assert "price = $49" in by_id["Product:starter plan"]["title"]
    assert len([e for e in edges if e.get("dashes")]) == 3
    [relation] = [e for e in edges if e.get("label")]
    assert relation["label"] == "OFFERS" and relation["arrows"] == "to"


def test_page_embeds_data_without_closing_its_script():
    """Evidence text cannot end the inline <script> early."""
    page = render(GRAPH)
    script = re.search(r"const data = (.*?);\n", page).group(1)
    assert "</script>" not in script
    assert "2 documents, 2 entities, 1 relations" in page


def test_cli_visualize_writes_the_page(monkeypatch, tmp_path, capsys):
    """`nexo visualize --out` exports and reports what the page contains."""
    seen = {}

    def fake_export(out, *, open_browser):
        seen.update(out=out, open_browser=open_browser)
        return {"documents": 2, "entities": 2, "relations": 1}

    monkeypatch.setattr(cli, "export", fake_export)
    out = tmp_path / "g.html"
    assert cli.main(["visualize", "--out", str(out), "--open"]) == 0
    assert seen == {"out": out, "open_browser": True}
    assert json.loads(capsys.readouterr().out)["entities"] == 2
