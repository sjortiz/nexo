"""The components `nexo setup` knows: their compose and collector fragments, secrets and memory.

Fragments are plain YAML under `deploy/`; the images pinned in them are the defaults that
`nexo setup lock --upgrade` moves to.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from nexo.setup.schema import SetupError


@dataclass(frozen=True)
class ComponentDef:
    """One deployable piece of the stack."""

    name: str
    # Compose fragment (services, volumes) and collector config fragment, repo-relative.
    compose: str | None = None
    collector: str | None = None
    # The service that `image` and `env` overrides apply to.
    main: str | None = None
    # Environment the component needs on the collector (its exporter's credentials).
    collector_env: dict[str, str] = field(default_factory=dict)
    # Generated into .env by `nexo setup secrets`.
    secrets: tuple[str, ...] = ()
    # Set by the user in .env; checked, never generated.
    required: tuple[str, ...] = ()


COMPONENTS: dict[str, ComponentDef] = {
    c.name: c
    for c in (
        ComponentDef(
            "neo4j",
            compose="deploy/compose/neo4j.yaml",
            main="neo4j",
            secrets=("NEO4J_PASSWORD",),
        ),
        ComponentDef(
            "collector",
            compose="deploy/compose/otel-collector.yaml",
            main="otel-collector",
        ),
        ComponentDef(
            "signoz",
            compose="deploy/signoz/compose.yml",
            main="signoz",
            secrets=(
                "SIGNOZ_USER_PASSWORD",
                "SIGNOZ_JWT_SECRET",
                "SIGNOZ_POSTGRES_PASSWORD",
            ),
        ),
        ComponentDef(
            "langfuse",
            compose="deploy/langfuse/compose.yml",
            collector="deploy/otel-addons/langfuse.yaml",
            main="langfuse-web",
            collector_env={
                "LANGFUSE_PUBLIC_KEY": "${LANGFUSE_PUBLIC_KEY:?run make env}",
                "LANGFUSE_SECRET_KEY": "${LANGFUSE_SECRET_KEY:?run make env}",
            },
            secrets=(
                "LANGFUSE_PUBLIC_KEY",
                "LANGFUSE_SECRET_KEY",
                "LANGFUSE_USER_PASSWORD",
                "LANGFUSE_SALT",
                "LANGFUSE_ENCRYPTION_KEY",
                "LANGFUSE_NEXTAUTH_SECRET",
                "LANGFUSE_POSTGRES_PASSWORD",
                "LANGFUSE_CLICKHOUSE_PASSWORD",
                "LANGFUSE_REDIS_PASSWORD",
                "LANGFUSE_S3_SECRET",
            ),
        ),
        ComponentDef(
            "jaeger",
            compose="deploy/compose/jaeger.yaml",
            collector="deploy/otel-addons/jaeger.yaml",
            main="jaeger",
        ),
        ComponentDef(
            "langsmith",
            collector="deploy/otel-addons/langsmith.yaml",
            collector_env={
                "LANGSMITH_API_KEY": "${LANGSMITH_API_KEY:?set LANGSMITH_API_KEY in .env}"
            },
            required=("LANGSMITH_API_KEY",),
        ),
        ComponentDef(
            "agentInbox",
            compose="deploy/agent-inbox/compose.yml",
            main="agent-inbox",
        ),
        ComponentDef(
            "hostMetrics",
            compose="deploy/compose/host-metrics.yaml",
            collector="deploy/otel-addons/host-metrics.yaml",
        ),
        ComponentDef(
            "vllm",
            compose="deploy/compose/vllm.yaml",
            main="vllm-inference-service",
            secrets=("VLLM_API_KEY", "EMBEDDING_API_KEY"),
            required=("HF_TOKEN",),
        ),
        ComponentDef("app", compose="deploy/compose/nexo.yaml", main="nexo"),
    )
}
DEFAULT_ON = {"neo4j", "collector", "signoz", "hostMetrics", "app"}
# Resident memory measured on a 24 GB Apple Silicon Mac after an ingest (podman stats,
# upper end of the range), for the preflight budget.
MEMORY_GB = {
    "neo4j": 0.63,
    "collector": 0.05,
    "signoz": 1.4,  # ClickHouse ~1.15, keeper, ingester, app and Postgres
    "langfuse": 3.0,  # web 1.4, worker 0.8, ClickHouse 0.65, storage, Postgres, Redis
    "jaeger": 0.01,
    "agentInbox": 0.15,  # the Next.js server; the adapter runs on the host
}
# Web UIs up prints: component -> its spec.ports field.
UIS = {
    "neo4j": "neo4j_http",
    "signoz": "signoz",
    "langfuse": "langfuse",
    "jaeger": "jaeger",
}


def load_fragment(root: Path, relative: str) -> dict[str, Any]:
    """A repo YAML fragment as a dict."""
    path = root / relative
    if not path.exists():
        raise SetupError(f"{relative} not found: run nexo setup from the Nexo checkout")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def default_images(root: Path, name: str) -> dict[str, str]:
    """Service name -> image pinned in a component's compose fragment."""
    component = COMPONENTS[name]
    if component.compose is None:
        return {}
    services = load_fragment(root, component.compose).get("services", {})
    return {svc: spec["image"] for svc, spec in services.items() if "image" in spec}


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386 JSON Merge Patch: maps merge, null deletes, lists and scalars replace."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def merge_compose(base: Any, extra: Any) -> Any:
    """Compose-style merge of fragments: maps merge, lists append (no duplicates)."""
    if isinstance(base, dict) and isinstance(extra, dict):
        merged = dict(base)
        for key, value in extra.items():
            merged[key] = merge_compose(base[key], value) if key in base else value
        return copy.deepcopy(merged)
    if isinstance(base, list) and isinstance(extra, list):
        return base + [item for item in extra if item not in base]
    return copy.deepcopy(extra)
