"""After `compose up -d`: wait until the services run, and explain the ones that do not."""

from __future__ import annotations

import json
import re
from typing import Any

# Log lines that mean a database refused the password `.env` holds now; the first is
# Postgres's wording, so its data volume is the one to look at.
POSTGRES_AUTH = re.compile(r"password authentication failed|SASL auth", re.IGNORECASE)
AUTH_FAILURE = re.compile(
    POSTGRES_AUTH.pattern + r"|authentication failed|Access denied", re.IGNORECASE
)


def parse_ps(text: str) -> list[dict[str, Any]]:
    """`compose ps --all --format json` output: a JSON array, or one object per line."""
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def assess(containers: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """(failed, still starting) service names; one-shot jobs that exited 0 are done."""
    failed, waiting = [], []
    for container in containers:
        name = container.get("Service") or container.get("Name", "?")
        state = str(container.get("State", "")).lower()
        health = str(container.get("Health", "")).lower()
        exited_badly = state == "exited" and int(container.get("ExitCode") or 0) != 0
        if exited_badly or state in {"dead", "restarting"} or health == "unhealthy":
            failed.append(name)
        elif state in {"created", "starting"} or health == "starting":
            waiting.append(name)
    return sorted(set(failed)), sorted(set(waiting))


def stale_volume_hint(
    service: str, logs: str, compose: dict[str, Any], engine: str
) -> str | None:
    """When a service's logs show a refused password, name its data volumes and the fix."""
    if not AUTH_FAILURE.search(logs):
        return None
    services = compose["services"]
    # The refusing database is the service itself or one it depends on.
    candidates = [service, *services.get(service, {}).get("depends_on", [])]
    if POSTGRES_AUTH.search(logs):
        candidates = [c for c in candidates if "postgres" in c] or candidates
    named = set(compose.get("volumes", {}))
    volumes = [
        f"{compose['name']}_{mount.split(':', 1)[0]}"
        for candidate in candidates
        for mount in services.get(candidate, {}).get("volumes", [])
        if isinstance(mount, str) and mount.split(":", 1)[0] in named
    ]
    where = " ".join(volumes) or "<its data volume>"
    return (
        f"{service} refused the password in .env: its data volume was created with an older"
        f" .env. Restore the old value in .env, or delete the volume (its data goes):"
        f" {engine} volume rm {where}"
    )
