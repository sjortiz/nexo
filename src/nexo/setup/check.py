"""Preflight: what this machine has, and whether the setup fits it (foundry's gauge)."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from neo4j import GraphDatabase
from neo4j.exceptions import AuthError, DriverError, Neo4jError

from nexo.local_profiles import MODEL_SIZES_GB, parse_profile
from nexo.secret_store import SecretStore, SecretStoreError
from nexo.setup.components import MEMORY_GB
from nexo.setup.envfile import generated_keys, missing_required, read_env
from nexo.setup.render import RENDER_DIR, Host
from nexo.setup.schema import Casting
from nexo.setup.status import parse_ps

# Share of memory the setup may use: the rest is for the system and your apps.
MEMORY_SHARE, VM_SHARE = 0.85, 0.9
CDI_DIRS = ("/etc/cdi", "/var/run/cdi")
# Findings that stop check and up unless --force: the machine may still cope.
FORCEABLE = {"profile", "memory", "podman vm", "host", "gpu", "ports"}


@dataclass(frozen=True)
class Finding:
    """One preflight line."""

    name: str
    ok: bool
    detail: str


def _run(*command: str) -> str:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def detect_host(casting: Casting) -> Host:
    """The OS, container engine and its socket, unless the setup file fixes them."""
    target = casting.spec.target
    os_name = target.host
    if os_name == "auto":
        os_name = "macos" if platform.system() == "Darwin" else "linux"
    engine = target.engine
    if engine == "auto":
        engine = "podman" if shutil.which("podman") else "docker"
    sock = "/var/run/docker.sock"
    if engine == "podman":
        path = _run("podman", "info", "--format", "{{.Host.RemoteSocket.Path}}")
        sock = path.removeprefix("unix://") or sock
    return Host(
        os=os_name,
        engine=engine,
        socket=sock,
        hostname=socket.gethostname().split(".")[0],
    )


def machine_memory_gb() -> float | None:
    """This machine's physical memory in GiB (how profiles name it: 24 on a 24 GB Mac)."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    except (ValueError, OSError, AttributeError):
        return None


def engine_memory_gb(host: Host) -> float | None:
    """The Podman VM's memory limit on macOS (containers cannot use more)."""
    if host.engine != "podman" or host.os != "macos":
        return None
    mib = _run("podman", "machine", "inspect", "--format", "{{.Resources.Memory}}")
    return int(mib.splitlines()[0]) / 1024 if mib else None


def ollama_models(port: int) -> set[str] | None:
    """Models this machine's Ollama has pulled; None when it is not running."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/tags", timeout=2
        ) as response:
            return {m["name"] for m in json.load(response).get("models", [])}
    except (OSError, ValueError):
        return None


def models_finding(
    casting: Casting, lock: dict[str, Any], pulled=ollama_models
) -> Finding | None:
    """Which models this machine serves, and which `up` still has to pull."""
    models = casting.spec.models
    if models.provider != "ollama":
        return None
    local_server = f"http://localhost:{models.local.port}/v1"
    served = sorted(
        {
            entry["model"]
            for entry in lock["models"].values()
            if entry["server"] == local_server and entry["model"]
        }
    )
    size = round(sum(MODEL_SIZES_GB.get(m, 0.0) for m in served), 1)
    detail = f"this machine serves {', '.join(served)} ({size} GB)"
    have = pulled(models.local.port)
    if have is None:
        detail += "; Ollama is not running (up starts it and pulls what is missing)"
    else:
        missing = [m for m in served if m not in have and f"{m}:latest" not in have]
        if missing:
            sizes = ", ".join(f"{m} ({MODEL_SIZES_GB.get(m, 0.0)} GB)" for m in missing)
            detail += f"; up will pull {sizes}"
    return Finding("models", True, detail)


def memory_budget(
    casting: Casting, lock: dict[str, Any], on: set[str]
) -> dict[str, float]:
    """GB this machine needs: model weights it serves plus each enabled container."""
    budget = {name: MEMORY_GB[name] for name in sorted(on) if name in MEMORY_GB}
    models = casting.spec.models
    if models.provider == "ollama":
        local_server = f"http://localhost:{models.local.port}/v1"
        served = {
            entry["model"]
            for entry in lock["models"].values()
            if entry["server"] == local_server and entry["model"]
        }
        budget["models"] = round(sum(MODEL_SIZES_GB.get(m, 0.0) for m in served), 1)
    return budget


def neo4j_login(uri: str, user: str, password: str) -> str | None:
    """None when the login works; "refused" or "unreachable" otherwise (no secret shown)."""
    try:
        with GraphDatabase.driver(
            uri, auth=(user, password), connection_timeout=5
        ) as driver:
            driver.verify_connectivity()
    except AuthError:
        return "refused"
    except (Neo4jError, DriverError, OSError):
        return "unreachable"
    return None


def neo4j_finding(
    store: SecretStore, host: Host, stack: str, uri: str
) -> Finding | None:
    """A running Neo4j accepts the password the provider holds (its healthcheck is HTTP only)."""
    env_path = Path.cwd() / ".env"
    dot_env = read_env(env_path)
    password = store.get(["NEO4J_PASSWORD"]).get("NEO4J_PASSWORD")
    if not password:
        return None  # up creates it
    result = neo4j_login(uri, dot_env.get("NEO4J_USER") or "neo4j", password)
    if result == "unreachable":
        return None  # not running yet
    if result is None:
        return Finding("neo4j", True, f"login ok at {uri}")
    return Finding("neo4j", False, stale_neo4j_hint(store, host, stack))


def stale_neo4j_hint(store: SecretStore, host: Host, stack: str) -> str:
    """Why Neo4j refused the provider's password, and the two ways out."""
    return (
        f"Neo4j refused the password from {store.label}: its data volume was created with"
        f" other secrets. Put that password back in {store.label}, or delete the volume"
        f" (its data goes): {host.engine} volume rm {stack}_neo4j-data"
    )


def _listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _container_ports(engine: str) -> dict[int, tuple[str, str]]:
    """Host port -> (compose project, container) for the engine's running containers."""
    owners: dict[int, tuple[str, str]] = {}
    for container in parse_ps(_run(engine, "ps", "--format", "json")):
        labels = container.get("Labels") or {}
        if isinstance(labels, str):  # docker: "key=value,key=value"
            labels = dict(item.partition("=")[::2] for item in labels.split(","))
        names = container.get("Names") or [""]
        name = names[0] if isinstance(names, list) else names
        ports = container.get("Ports") or []
        if isinstance(ports, str):  # docker: "127.0.0.1:7687->7687/tcp, ..."
            ports = [{"host_port": p} for p in re.findall(r":(\d+)->", ports)]
        for port in ports:
            for offset in range(int(port.get("range") or 1)):
                owners[int(port["host_port"]) + offset] = (
                    labels.get("com.docker.compose.project", ""),
                    name,
                )
    return owners


def _process_on(port: int) -> str:
    """The command listening on a port, from lsof (blank when unknown)."""
    fields = _run("lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fc").splitlines()
    return next((f[1:] for f in fields if f.startswith("c")), "")


def ports_finding(compose: dict[str, Any], host: Host) -> Finding:
    """Every host port the render publishes is free or held by this stack."""
    busy = []
    owners: dict[int, tuple[str, str]] | None = None
    for service in compose["services"].values():
        for mapping in service.get("ports", []):
            port = int(mapping.rsplit(":", 2)[-2])
            if not _listening(port):
                continue
            if owners is None:
                owners = _container_ports(host.engine)
            project, name = owners.get(port, ("", ""))
            if project != compose["name"]:
                owner = f"{name}, stack {project}" if name else _process_on(port)
                busy.append(f"{port} ({owner or 'another process'})")
    if busy:
        return Finding(
            "ports", False, f"in use: {', '.join(busy)}; set spec.ports in nexo.yaml"
        )
    return Finding("ports", True, "free, or already this stack's")


def secrets_finding(store: SecretStore, on: set[str], env_path: Path) -> Finding:
    """The provider answers, and nothing up cannot create itself is missing."""
    needed = generated_keys(on)
    try:
        found = store.get(needed)
    except SecretStoreError as exc:
        return Finding("secrets", False, str(exc))
    missing = missing_required(env_path, on)
    if not store.writable:  # mounted by the platform: up cannot create them
        missing += [k for k in needed if k not in found]
    detail = store.label + (f"; set: {', '.join(missing)}" if missing else "")
    return Finding("secrets", not missing, detail)


REVIEW_POLICIES = {
    "always": "every answer waits for a person",
    "on-findings": (
        "answers with no findings and grounded citations are approved as system:auto;"
        " the rest wait for a person"
    ),
    "never": "answers with no findings or only warnings are approved as system:auto",
}


def review_finding(casting: Casting) -> Finding:
    """The review policy answers will run under (spec.review, unless the environment wins).

    nexo.env always carries spec.review and is read after .env, so only the process
    environment can override it.
    """
    declared = casting.spec.review
    effective = os.environ.get("REVIEW_POLICY") or declared
    detail = f"{effective}: {REVIEW_POLICIES[effective]}"
    if effective != declared:
        detail += (
            f" (REVIEW_POLICY in the environment overrides spec.review {declared})"
        )
    return Finding("review", True, detail)


def preflight(
    casting: Casting,
    lock: dict[str, Any],
    on: set[str],
    host: Host,
    store: SecretStore,
) -> list[Finding]:
    """Everything `up` needs from this machine, without starting anything."""
    findings = [
        Finding(
            "engine",
            bool(_run(host.engine, "compose", "version")),
            f"{host.engine} compose ({host.os})",
        ),
        secrets_finding(store, on, Path.cwd() / ".env"),
        review_finding(casting),
    ]
    bolt = casting.spec.ports.bolt
    if "neo4j" in on and (neo4j := neo4j_finding(store, host, lock["name"], bolt)):
        findings.append(neo4j)
    rendered = Path.cwd() / RENDER_DIR / "compose.yaml"
    if rendered.exists():
        findings.append(ports_finding(yaml.safe_load(rendered.read_text()), host))
    models = casting.spec.models
    if models.provider == "ollama":
        ollama = shutil.which("ollama")
        findings.append(
            Finding(
                "ollama",
                bool(ollama),
                ollama or "not installed: brew install ollama, or see ollama.com",
            )
        )
        if models.remote is not None:
            remote = models.remote
            findings.append(
                Finding(
                    "remote",
                    True,
                    "on the remote machine: deploy/local/ollama-setup.sh"
                    f" {remote.profile} heavy; here:"
                    f" ssh -N -L {remote.port}:127.0.0.1:11434 <remote host>",
                )
            )
    findings += [f for f in (models_finding(casting, lock),) if f]
    findings += platform_findings(casting, host)
    findings += memory_findings(casting, lock, on, host)
    return findings


def this_os() -> str:
    """The OS this process runs on, in the setup file's terms."""
    return "macos" if platform.system() == "Darwin" else "linux"


def _gpu_finding(casting: Casting, host: Host) -> Finding | None:
    """Whether the GPU the setup expects is there (and usable by containers, for vLLM)."""
    models = casting.spec.models
    if models.provider == "vllm":
        if not shutil.which("nvidia-smi"):
            return Finding("gpu", False, "vLLM needs NVIDIA GPUs; nvidia-smi not found")
        if host.engine == "docker":
            usable = "nvidia" in _run(
                "docker", "info", "--format", "{{json .Runtimes}}"
            )
            how = "the NVIDIA Container Toolkit (docker runtime)"
        else:
            usable = any(Path(d, "nvidia.yaml").exists() for d in CDI_DIRS)
            how = (
                "a CDI spec: sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml"
            )
        return Finding(
            "gpu", usable, "NVIDIA GPUs for containers" if usable else f"needs {how}"
        )
    if models.local is not None and parse_profile(models.local.profile).kind == "gpu":
        tool = shutil.which("nvidia-smi") or shutil.which("rocm-smi")
        return Finding(
            "gpu",
            bool(tool),
            tool or f"{models.local.profile} expects a GPU; no nvidia-smi or rocm-smi",
        )
    return None


def platform_findings(casting: Casting, host: Host) -> list[Finding]:
    """The setup targets this OS, and the GPU it expects is there."""
    actual = this_os()
    findings = [
        Finding(
            "host",
            host.os == actual,
            f"rendered for {host.os}"
            + (
                ""
                if host.os == actual
                else f"; this machine runs {actual}: set target.host"
            ),
        )
    ]
    gpu = _gpu_finding(casting, host)
    return findings + ([gpu] if gpu else [])


def memory_findings(
    casting: Casting, lock: dict[str, Any], on: set[str], host: Host
) -> list[Finding]:
    """Does the setup fit this machine (its real memory) and the Podman VM?"""
    budget = memory_budget(casting, lock, on)
    containers = round(sum(v for k, v in budget.items() if k != "models"), 1)
    total = round(sum(budget.values()), 1)
    parts = ", ".join(f"{k} {v}" for k, v in budget.items())
    machine = machine_memory_gb()
    models = casting.spec.models
    findings = []
    if models.local is not None and machine is not None:
        claimed = parse_profile(models.local.profile).memory_gb
        findings.append(
            Finding(
                "profile",
                claimed <= machine * 1.1,
                f"{models.local.profile} is for {claimed} GB; this machine has"
                f" {machine:.0f} GB",
            )
        )
    findings.append(
        Finding(
            "memory",
            machine is None or total <= machine * MEMORY_SHARE,
            (
                f"{total} GB of {machine:.0f} GB ({parts}); at most"
                f" {MEMORY_SHARE:.0%} leaves room for the system"
                if machine
                else f"{total} GB ({parts})"
            ),
        )
    )
    vm = engine_memory_gb(host)
    if vm is not None:
        findings.append(
            Finding(
                "podman vm",
                containers <= vm * VM_SHARE,
                f"containers {containers} GB of {vm:.1f} GB"
                " (podman machine set --memory to raise)",
            )
        )
    return findings
