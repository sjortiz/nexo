"""The committed dashboard and alerts load into SigNoz by name, idempotently."""

import json
from pathlib import Path

from nexo.setup.signoz import CHANNEL, DISCARD, PROVISION, provision

ROOT = Path(__file__).resolve().parent.parent


class _FakeSignoz:
    """Keeps dashboards, channels and rules in memory, like the SigNoz API does."""

    def __init__(self) -> None:
        self.store = {"dashboards": [], "channels": [], "rules": []}
        self.calls: list[tuple[str, str]] = []
        self.next_id = 0

    def call(self, method, path, body=None):
        """The handful of routes provision() uses."""
        self.calls.append((method, path))
        kind = path.split("/")[3]
        if method == "GET":
            items = self.store[kind]
            return (
                {"dashboards": items}
                if kind == "dashboards"
                else ({"rules": items} if kind == "rules" else items)
            )
        if method == "DELETE":
            ident = path.rsplit("/", 1)[1]
            if kind == "channels":
                assert not self.store["rules"], "a rule still routes to this channel"
            self.store[kind] = [i for i in self.store[kind] if i["id"] != ident]
            return None
        self.next_id += 1
        item = {"id": str(self.next_id), **body}
        if kind == "dashboards":
            item["spec"] = {"display": {"name": body["title"]}}
        self.store[kind].append(item)
        return item


def test_provision_replaces_by_name_and_routes_rules_to_the_nexo_channel():
    """Twice in a row leaves one dashboard, one channel and one copy of each rule."""
    fake = _FakeSignoz()
    fake.store["dashboards"].append(
        {"id": "keep", "spec": {"display": {"name": "Other"}}}
    )
    provision(fake, ROOT)
    provision(fake, ROOT, webhook="https://hooks.example/nexo")
    rules = json.loads((ROOT / PROVISION / "alerts.json").read_text())
    names = sorted(d["spec"]["display"]["name"] for d in fake.store["dashboards"])
    assert names == ["Nexo", "Other"]
    assert len(fake.store["channels"]) == 1
    channel = fake.store["channels"][0]
    assert channel["name"] == CHANNEL
    assert channel["webhook_configs"][0]["url"] == "https://hooks.example/nexo"
    assert sorted(r["alert"] for r in fake.store["rules"]) == sorted(
        r["alert"] for r in rules
    )
    assert all(r["preferredChannels"] == [CHANNEL] for r in fake.store["rules"])


def test_without_a_webhook_alerts_go_to_the_discard_address():
    """Rules need a channel; with none configured they are shown, not sent."""
    fake = _FakeSignoz()
    provision(fake, ROOT)
    assert fake.store["channels"][0]["webhook_configs"][0]["url"] == DISCARD


def test_every_panel_and_rule_reads_spans_in_the_selected_window():
    """Each committed query is ClickHouse SQL bounded by the dashboard's time range."""
    dashboard = json.loads((ROOT / PROVISION / "dashboard.json").read_text())
    rules = json.loads((ROOT / PROVISION / "alerts.json").read_text())
    queries = [w["query"]["clickhouse_sql"][0]["query"] for w in dashboard["widgets"]]
    queries += [
        r["condition"]["compositeQuery"]["queries"][0]["spec"]["query"] for r in rules
    ]
    assert len(dashboard["widgets"]) == len(dashboard["layout"]) == 9
    assert all("{{.start_datetime}}" in q and "{{.end_datetime}}" in q for q in queries)
