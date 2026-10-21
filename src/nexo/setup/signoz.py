"""Load the committed Nexo dashboard and alerts into SigNoz, headless, at `nexo setup up`.

SigNoz has no file-based provisioning, so this logs in as the root user the compose
file creates and replaces, by name, the dashboard and the rules in
deploy/signoz/provision/. Rules need a notification channel: a webhook named `nexo`
at SIGNOZ_ALERT_WEBHOOK, or a discard address when unset, so the alerts still show
as firing on SigNoz's Alerts page.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PROVISION = Path("deploy/signoz/provision")
CHANNEL = "nexo"
DISCARD = (
    "http://127.0.0.1:9/nexo-alerts"  # port 9 discards: alerts show, nobody is paged
)


class SignozError(RuntimeError):
    """SigNoz refused a provisioning call."""


class SignozClient:
    """The few SigNoz API calls provisioning needs, as the root user."""

    def __init__(self, base_url: str, timeout_s: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.token: str | None = None

    def call(self, method: str, path: str, body: Any = None) -> Any:
        """One JSON request; the decoded `data` of the reply."""
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            self.base_url + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as reply:
                raw = reply.read()
        except urllib.error.HTTPError as exc:
            raise SignozError(
                f"{method} {path}: {exc.code} {exc.read()[:200]!r}"
            ) from exc
        return json.loads(raw).get("data") if raw else None

    def login(self, email: str, password: str) -> None:
        """A session token for the root user of the `nexo` organization."""
        context = self.call(
            "GET", f"/api/v2/sessions/context?email={email}&ref={self.base_url}"
        )
        org = context["orgs"][0]["id"]
        session = self.call(
            "POST",
            "/api/v2/sessions/email_password",
            {"email": email, "password": password, "orgId": org},
        )
        self.token = session["accessToken"]


def provision(
    client: SignozClient, root: Path, webhook: str | None = None
) -> dict[str, int]:
    """Replace the Nexo dashboard, the `nexo` channel and the Nexo rules; say how many."""
    dashboard = json.loads((root / PROVISION / "dashboard.json").read_text())
    rules = json.loads((root / PROVISION / "alerts.json").read_text())
    for existing in client.call("GET", "/api/v2/dashboards")["dashboards"]:
        if existing["spec"]["display"]["name"] == dashboard["title"]:
            client.call("DELETE", f"/api/v2/dashboards/{existing['id']}")
    client.call("POST", "/api/v2/dashboards", dashboard)

    # Rules first: SigNoz will not delete a channel a rule still routes to.
    names = {rule["alert"] for rule in rules}
    for existing in client.call("GET", "/api/v1/rules")["rules"]:
        if existing["alert"] in names:
            client.call("DELETE", f"/api/v1/rules/{existing['id']}")
    for channel in client.call("GET", "/api/v1/channels") or []:
        if channel["name"] == CHANNEL:
            client.call("DELETE", f"/api/v1/channels/{channel['id']}")
    client.call(
        "POST",
        "/api/v1/channels",
        {
            "name": CHANNEL,
            "webhook_configs": [{"send_resolved": True, "url": webhook or DISCARD}],
        },
    )
    for rule in rules:
        client.call("POST", "/api/v1/rules", {**rule, "preferredChannels": [CHANNEL]})
    return {"dashboards": 1, "rules": len(rules)}
