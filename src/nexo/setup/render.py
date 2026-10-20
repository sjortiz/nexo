"""Render a setup file and its lock into plain files under `.nexo/render/`.

    compose.yaml         the services this setup turns on, images from the lock
    otel-collector.yaml  the base collector config plus each enabled add-on's pipeline
    nexo.env             what Nexo reads after .env: model servers, profiles, endpoints

The folder holds machine-specific values (paths, the container socket) and no secrets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonpatch
import yaml

from nexo.setup.components import COMPONENTS, load_fragment, merge_compose, merge_patch
from nexo.setup.lock import VLLM_EMBEDDING_URL, VLLM_URL
from nexo.setup.schema import Casting, SetupError

COLLECTOR_BASE = "deploy/otel-collector.yaml"
RENDER_DIR = ".nexo/render"
# Order of services in compose.yaml and of pipelines in the collector config.
ORDER = (
    "neo4j",
    "collector",
    "signoz",
    "hostMetrics",
    "langfuse",
    "jaeger",
    "vllm",
    "app",
)
COLLECTOR_ADDONS = ("hostMetrics", "langfuse", "jaeger", "langsmith")


@dataclass(frozen=True)
class Host:
    """Facts about the machine the stack renders for."""

    os: str  # macos | linux
    engine: str  # podman | docker
    socket: str  # the engine's API socket as containers see it
    hostname: str

    @property
    def gateway(self) -> str:
        """How a container reaches services on the host (macOS)."""
        return (
            "host.containers.internal"
            if self.engine == "podman"
            else "host.docker.internal"
        )


def _substitute(value: Any, variables: dict[str, str]) -> Any:
    """Resolve ${NAME} and ${NAME:-default} for render-time names; others stay for compose."""
    if isinstance(value, dict):
        return {k: _substitute(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, variables) for v in value]
    if isinstance(value, str):
        pattern = r"\$\{(" + "|".join(variables) + r")(?::-[^}]*)?\}"
        return re.sub(pattern, lambda m: variables[m.group(1)], value)
    return value


def _absolute(source: str, base: Path) -> str:
    return str((base / source).resolve()) if source.startswith(".") else source


def _absolutize(service: dict[str, Any], base: Path) -> dict[str, Any]:
    """Make a fragment's relative bind mounts, build context and env files absolute."""
    volumes = []
    for volume in service.get("volumes", []):
        if isinstance(volume, str):
            source, _, rest = volume.partition(":")
            volumes.append(f"{_absolute(source, base)}:{rest}" if rest else volume)
        else:
            volumes.append(volume | {"source": _absolute(volume["source"], base)})
    if volumes:
        service["volumes"] = volumes
    if isinstance(service.get("build"), str):
        service["build"] = _absolute(service["build"], base)
    return service


def _compose_fragment(root: Path, name: str, images: dict[str, str]) -> dict[str, Any]:
    component = COMPONENTS[name]
    fragment = load_fragment(root, component.compose)
    base = (root / component.compose).parent
    services = fragment.get("services", {})
    for service_name, service in services.items():
        _absolutize(service, base)
        if service_name in images:
            service["image"] = images[service_name]
    return {k: v for k, v in fragment.items() if not k.startswith("x-")}


def _app_environment(casting: Casting, host: Host) -> dict[str, str]:
    """Endpoints as the nexo container sees them (macOS: through the engine's gateway)."""
    models = casting.spec.models
    env = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector:4318",
        "NEO4J_URI": "neo4j://neo4j:7687",
    }
    if models.local is not None:
        env["LOCAL_BASE_URL"] = f"http://{host.gateway}:{models.local.port}/v1"
    if models.remote is not None:
        env["REMOTE_BASE_URL"] = f"http://{host.gateway}:{models.remote.port}/v1"
    if models.provider == "vllm":
        env["VLLM_BASE_URL"] = "http://vllm-inference-service:8000/v1"
        env["EMBEDDING_BASE_URL"] = "http://vllm-embedding-service:8000/v1"
    return env


def _apply_environment(
    services: dict[str, Any], casting: Casting, on: set[str]
) -> None:
    """Each component's `env` override, and the credentials add-ons need on the collector."""
    overrides = casting.spec.components | casting.spec.addons
    collector = services["otel-collector"].setdefault("environment", {})
    for name in on:
        component = COMPONENTS[name]
        if name in overrides and overrides[name].env:
            main = services[component.main].setdefault("environment", {})
            main.update(overrides[name].env)
        collector.update(component.collector_env)
    if "langsmith" in on:
        langsmith = casting.spec.addons.get("langsmith")
        collector["LANGSMITH_PROJECT"] = (langsmith and langsmith.project) or "nexo"
    if not collector:
        del services["otel-collector"]["environment"]


def build_compose(
    casting: Casting, lock: dict[str, Any], on: set[str], root: Path, host: Host
) -> dict[str, Any]:
    """compose.yaml for the enabled components; files it mounts are in root/.nexo/render."""
    out = (root / RENDER_DIR).resolve()
    compose: dict[str, Any] = {"name": lock["name"]}
    for name in ORDER:
        if name in on and COMPONENTS[name].compose:
            fragment = _compose_fragment(root, name, lock["images"].get(name, {}))
            compose = merge_compose(compose, fragment)
    services = compose["services"]
    _apply_environment(services, casting, on)
    services["otel-collector"].setdefault("volumes", []).insert(
        0, f"{out / 'otel-collector.yaml'}:/etc/otelcol/config.yaml:ro"
    )
    app = services["nexo"]
    app["env_file"] = [
        {"path": str((root / ".env").resolve()), "required": False},
        {"path": str(out / "nexo.env"), "required": True},
    ]
    # Rootless Podman on Linux cannot reach the host's 127.0.0.1 from a bridge network.
    if host.os == "linux":
        app["network_mode"] = "host"
    else:
        app["environment"] = _app_environment(casting, host)
    if casting.spec.models.provider == "vllm":
        _configure_vllm(services, casting.spec.models.vllm)
    return _substitute(
        compose, {"CONTAINER_SOCKET": host.socket, "NEXO_HOST": host.hostname}
    )


def _set_flags(command: list[str], values: dict[str, str]) -> list[str]:
    return [
        f"{flag}={values[flag]}" if (flag := arg.split("=", 1)[0]) in values else arg
        for arg in command
    ]


def _configure_vllm(services: dict[str, Any], vllm: Any) -> None:
    """The models and GPU count from models.vllm."""
    inference = services["vllm-inference-service"]
    inference["command"] = _set_flags(
        inference["command"],
        {
            "--model": vllm.model,
            "--served-model-name": vllm.model,
            "--tensor-parallel-size": str(vllm.gpus),
        },
    )
    inference["deploy"]["resources"]["reservations"]["devices"][0]["count"] = vllm.gpus
    embedding = services["vllm-embedding-service"]
    embedding["command"] = _set_flags(embedding["command"], {"--model": vllm.embedding})


def build_collector(casting: Casting, on: set[str], root: Path) -> dict[str, Any]:
    """The collector config: base, each enabled add-on's fragment, then the setup's config."""
    config = load_fragment(root, COLLECTOR_BASE)
    for name in COLLECTOR_ADDONS:
        if name in on:
            config = merge_compose(
                config, load_fragment(root, COMPONENTS[name].collector)
            )
    override = casting.spec.components.get("collector")
    if override is not None and override.config:
        config = merge_patch(config, override.config)
    return config


def validate_collector(config: dict[str, Any]) -> None:
    """Every pipeline and extension refers to a defined component (otelcol would refuse)."""
    service = config.get("service", {})
    problems = [
        f"service.extensions: {ext}"
        for ext in service.get("extensions", [])
        if ext not in config.get("extensions", {})
    ]
    for pipeline, spec in service.get("pipelines", {}).items():
        for kind in ("receivers", "processors", "exporters"):
            problems += [
                f"{pipeline}.{kind}: {item}"
                for item in spec.get(kind, [])
                if item not in config.get(kind, {})
            ]
        if not spec.get("receivers") or not spec.get("exporters"):
            problems.append(f"{pipeline}: needs receivers and exporters")
    if problems:
        raise SetupError(
            "collector config refers to undefined components: " + "; ".join(problems)
        )


def build_env(casting: Casting) -> dict[str, str]:
    """Settings Nexo reads after .env (so the setup file wins); endpoints as the host sees them."""
    spec = casting.spec
    env = {
        "NEO4J_URI": "bolt://localhost:7687",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318",
        "OTEL_LOGS_EXPORTER": spec.telemetry.logs,
    }
    models = spec.models
    if models.provider == "ollama":
        env["LOCAL_PROFILE"] = models.local.profile
        env["LOCAL_BASE_URL"] = f"http://localhost:{models.local.port}/v1"
        if models.remote is not None:
            env["REMOTE_PROFILE"] = models.remote.profile
            env["REMOTE_BASE_URL"] = f"http://localhost:{models.remote.port}/v1"
    elif models.provider == "vllm":
        env |= {
            "VLLM_BASE_URL": VLLM_URL,
            "VLLM_MODEL": models.vllm.model,
            "EMBEDDING_BASE_URL": VLLM_EMBEDDING_URL,
            "EMBEDDING_MODEL": models.vllm.embedding,
        }
    for role, override in models.roles.items():
        if override.model:
            env[f"{role.upper()}_MODEL"] = override.model
        if override.base_url:
            env[f"{role.upper()}_BASE_URL"] = override.base_url
    return env


def _apply_patches(casting: Casting, files: dict[str, Any]) -> None:
    for patch in casting.spec.patches:
        if patch.target not in files or patch.target == "nexo.env":
            raise SetupError(
                f"patch target {patch.target!r}: expected one of compose.yaml, otel-collector.yaml"
            )
        try:
            files[patch.target] = jsonpatch.apply_patch(
                files[patch.target], patch.operations
            )
        except (jsonpatch.JsonPatchException, jsonpatch.JsonPointerException) as exc:
            raise SetupError(f"patch on {patch.target}: {exc}") from exc


def render(
    casting: Casting,
    lock: dict[str, Any],
    root: Path,
    host: Host,
    extra_addons: frozenset[str] = frozenset(),
) -> Path:
    """Write root/.nexo/render from the fragments under root/deploy; return its path."""
    on = enabled_set(lock, extra_addons)
    files: dict[str, Any] = {
        "compose.yaml": build_compose(casting, lock, on, root, host),
        "otel-collector.yaml": build_collector(casting, on, root),
    }
    _apply_patches(casting, files)
    validate_collector(files["otel-collector.yaml"])
    out = root / RENDER_DIR
    out.mkdir(parents=True, exist_ok=True)
    header = "# Rendered by `nexo setup render`; edit nexo.yaml instead.\n"
    for name, document in files.items():
        (out / name).write_text(
            header + yaml.safe_dump(document, sort_keys=False, width=100),
            encoding="utf-8",
        )
    env_lines = [f"{key}={value}" for key, value in build_env(casting).items()]
    (out / "nexo.env").write_text(
        header + "\n".join(env_lines) + "\n", encoding="utf-8"
    )
    return out


def enabled_set(lock: dict[str, Any], extra_addons: frozenset[str]) -> set[str]:
    """Components on for this run: the lock's plus per-run add-ons."""
    return {name for name, enabled in lock["enabled"].items() if enabled} | extra_addons
