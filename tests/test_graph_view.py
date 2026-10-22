"""`nexo visualize`: the knowledge graph as an interactive HTML page."""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request

import pytest

from nexo import cli, graph_view
from nexo.graph_view import live_payload, live_server, render, to_network

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

    def fake_export(out, *, open_browser, freeze):
        seen.update(out=out, open_browser=open_browser, freeze=freeze)
        return {"documents": 2, "entities": 2, "relations": 1}

    monkeypatch.setattr(cli, "export", fake_export)
    out = tmp_path / "g.html"
    assert cli.main(["visualize", "--out", str(out), "--open"]) == 0
    assert seen == {"out": out, "open_browser": True, "freeze": False}
    assert json.loads(capsys.readouterr().out)["entities"] == 2


def test_live_page_polls_and_live_payload_has_stable_unique_ids():
    """The live page re-reads graph.json; every node and edge has a stable id."""
    page = render(GRAPH, live_interval_s=5)
    assert 'fetch("graph.json"' in page and "5000" in page and 'id="updated"' in page
    assert "fetch(" not in render(GRAPH)  # static pages stay static
    doubled = {**GRAPH, "relations": GRAPH["relations"] * 2}
    payload = live_payload(doubled)
    ids = [e["id"] for e in payload["edges"]]
    assert len(ids) == len(set(ids)) and payload["summary"].startswith("2 documents")
    assert live_payload(doubled)["edges"] == payload["edges"]  # stable across reads


def test_live_server_serves_page_and_data(monkeypatch):
    """GET / and /graph.json come from the graph; anything else is 404."""
    monkeypatch.setattr(graph_view, "read_graph", lambda: GRAPH)
    server = live_server("127.0.0.1", 0, 5)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/graph.json") as response:
            assert json.load(response)["summary"].startswith("2 documents")
        with urllib.request.urlopen(base + "/") as response:
            assert b"Nexo graph" in response.read()
        with pytest.raises(urllib.error.HTTPError):
            with urllib.request.urlopen(base + "/other"):
                pass
    finally:
        server.shutdown()
        server.server_close()


def test_cli_live_refuses_non_loopback_hosts():
    """Live mode has no authentication, so it binds loopback only."""
    assert cli.main(["visualize", "--live", "--host", "0.0.0.0"]) == 2


def test_pause_button_and_freeze():
    """Every page can pause its layout; --freeze stops it once it settles."""
    assert "toggleLayout" in render(GRAPH) and "Pause layout" in render(GRAPH)
    assert "stabilizationIterationsDone" not in render(GRAPH)
    assert "stabilizationIterationsDone" in render(GRAPH, freeze=True)
    assert "stabilizationIterationsDone" in render(
        GRAPH, live_interval_s=5, freeze=True
    )
