"""Preflight: what this machine has, and whether the setup fits it (foundry's gauge)."""

from __future__ import annotations

import json
import math
import os
import platform
import re
import shutil
import socket
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml
from neo4j import GraphDatabase
from neo4j.exceptions import AuthError, DriverError, Neo4jError
from pydantic import ValidationError

from nexo.config import Settings
from nexo.local_profiles import (
    APPLE_MEMORY_GB,
    MODEL_SIZES_GB,
    parse_profile,
    plan,
)
from nexo.secret_store import SecretStore, SecretStoreError
from nexo.setup.components import MEMORY_GB
from nexo.setup.envfile import generated_keys, missing_required, read_env
from nexo.setup.engine import ROOTFUL_SOCKET, Machines, run_command
from nexo.setup.render import RENDER_DIR, Host, containers
from nexo.setup.schema import Casting
from nexo.setup.status import parse_ps

# Share of memory the setup may use: the rest is for the system and your apps.
MEMORY_SHARE, VM_SHARE = 0.85, 0.9
CDI_DIRS = ("/etc/cdi", "/var/run/cdi")
# Findings that stop check and up unless --force: the machine may still cope.
# A Podman VM this many times larger than the containers (and 4 GiB more) is oversized.
OVERSIZE_RATIO, OVERSIZE_GB = 3, 4
FORCEABLE = {"profile", "memory", "podman vm", "host", "gpu", "ports"}


@dataclass(frozen=True)
class Finding:
    """One preflight line."""

    name: str
    ok: bool
    detail: str
    # A problem worth a WARN line that never stops check or up.
    advisory: bool = False


def _run(*command: str) -> str:
    """A command's stdout; blank when it fails or is missing."""
    ran = run_command(command)
    return ran.out if ran.code == 0 else ""


def detect_host(casting: Casting, machines: Machines | None = None) -> Host:
    """The OS, container engine and its socket, unless the setup file fixes them.

    With a Podman machine named in the setup, its connection is pinned; on macOS the
    default machine is described too (memory, rootful), without pinning anything.
    """
    target = casting.spec.target
    os_name = target.host
    if os_name == "auto":
        os_name = "macos" if platform.system() == "Darwin" else "linux"
    engine = target.engine
    if engine == "auto":
        engine = "podman" if shutil.which("podman") else "docker"
    host = Host(
        os=os_name,
        engine=engine,
        socket="/var/run/docker.sock",
        hostname=socket.gethostname().split(".")[0],
    )
    if engine != "podman":
        return host
    if target.machine or os_name == "macos":
        machines = machines or Machines()
        machine = machines.inspect(target.machine or "")
        host = replace(
            host,
            machine=machine,
            running=machines.running(),
            connection=machine.connection if machine and target.machine else "",
        )
    if host.machine and host.connection and host.machine.rootful:
        return replace(host, socket=ROOTFUL_SOCKET)
    path = _run(*host.cli, "info", "--format", "{{.Host.RemoteSocket.Path}}")
    return replace(host, socket=path.removeprefix("unix://") or host.socket)


def machine_memory_gb() -> float | None:
    """This machine's physical memory in GiB (how profiles name it: 24 on a 24 GB Mac)."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    except (ValueError, OSError, AttributeError):
        return None


APPLE_CHIP = re.compile(r"Apple M(?P<generation>\d)(?: (?P<variant>Pro|Max|Ultra))?")


def detect_profile(
    system: str | None = None, run=_run, memory_gb=machine_memory_gb
) -> tuple[str, str] | None:
    """(profile, what it was read from) for this machine, or None when it is unknown.

    Apple Silicon: the chip and unified memory (m1-max-64). Linux: an NVIDIA GPU's VRAM
    (gpu-24), else RAM (cpu-64). None below the smallest tier.
    """
    system = system or platform.system()
    try:
        if system == "Darwin":
            chip = APPLE_CHIP.search(run("sysctl", "-n", "machdep.cpu.brand_string"))
            memory = memory_gb()
            if chip is None or memory is None:
                return None
            # The shipped size nearest to what the OS reports.
            size = min(APPLE_MEMORY_GB, key=lambda gb: abs(gb - memory))
            variant = f"-{chip['variant'].lower()}" if chip["variant"] else ""
            name = f"m{chip['generation']}{variant}-{size}"
            seen = f"{chip[0]}, {size} GB"
        else:
            vram = run(
                "nvidia-smi",
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
            ).splitlines()
            if vram:
                size = round(int(vram[0]) / 1024)
                name, seen = f"gpu-{size}", f"NVIDIA GPU, {size} GB of VRAM"
            elif (memory := memory_gb()) is not None:
                name, seen = (
                    f"cpu-{round(memory)}",
                    f"no GPU, {round(memory)} GB of RAM",
                )
            else:
                return None
        return parse_profile(name).name, seen
    except ValueError:  # ProfileError, or nvidia-smi output that is not a number
        return None


def engine_memory_gb(host: Host) -> float | None:
    """The Podman VM's memory limit (containers cannot use more); None without a VM."""
    return host.machine.memory_gb if host.machine else None


def ollama_models(port: int) -> set[str] | None:
    """Models this machine's Ollama has pulled; None when it is not running."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/tags", timeout=2
        ) as response:
            return {m["name"] for m in json.load(response).get("models", [])}
    except (OSError, ValueError):
        return None


def loaded_contexts(base_url: str) -> dict[str, int]:
    """Context length of each model an Ollama server has loaded (empty when unknown)."""
    root = base_url.rstrip("/").removesuffix("/v1")
    try:
        with urllib.request.urlopen(f"{root}/api/ps", timeout=2) as response:
            loaded = json.load(response).get("models", [])
    except (OSError, ValueError):
        return {}
    return {m["name"]: m["context_length"] for m in loaded if m.get("context_length")}


def context_problem(
    base_url: str, profile: str, role_set: str, loaded=loaded_contexts
) -> str | None:
    """Loaded models running with less context than the profile sets, or None.

    Ollama started outside ollama-setup.sh (the macOS app, a systemd unit) keeps its own
    defaults: 4096 tokens below ~23 GiB of GPU memory, which truncates prompts silently.
    """
    expected = plan(profile, role_set).context_length
    short = {m: n for m, n in loaded(base_url).items() if n < expected}
    if not short:
        return None
    runs = ", ".join(f"{m} with {n}" for m, n in sorted(short.items()))
    return (
        f"Ollama at {base_url} runs {runs} tokens of context; {profile} needs {expected}:"
        f" deploy/local/ollama-setup.sh {profile} {role_set} --apply, then restart Ollama"
    )


def context_findings(casting: Casting, loaded=loaded_contexts) -> list[Finding]:
    """A WARN per Ollama server whose loaded models run below the profile's context."""
    models = casting.spec.models
    if models.provider != "ollama":
        return []
    servers = [
        (models.local, "light" if models.remote is not None else "single"),
        (models.remote, "heavy"),
    ]
    findings = []
    for machine, role_set in servers:
        if machine is None:
            continue
        url = f"http://127.0.0.1:{machine.port}"
        if problem := context_problem(url, machine.profile, role_set, loaded):
            findings.append(Finding("ollama context", False, problem, advisory=True))
    return findings


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
    store: SecretStore, host: Host, stack: str, uri: str, *, native: bool = False
) -> Finding | None:
    """A running Neo4j accepts the password the provider holds (its healthcheck is HTTP only).

    Native: Neo4j is this machine's own service, so it must already answer.
    """
    env_path = Path.cwd() / ".env"
    dot_env = read_env(env_path)
    password = store.get(["NEO4J_PASSWORD"]).get("NEO4J_PASSWORD")
    if not password:
        return None  # up creates it
    result = neo4j_login(uri, dot_env.get("NEO4J_USER") or "neo4j", password)
    if result == "unreachable":
        if native:
            return Finding("neo4j", False, native_neo4j_hint(store, uri, result))
        return None  # not running yet
    if result is None:
        return Finding("neo4j", True, f"login ok at {uri}")
    if native:
        return Finding("neo4j", False, native_neo4j_hint(store, uri, result))
    return Finding("neo4j", False, stale_neo4j_hint(store, host, stack))


def native_neo4j_hint(store: SecretStore, uri: str, result: str) -> str:
    """target.mode native: how to start this machine's Neo4j or give it the stored password."""
    if result == "unreachable":
        return (
            f"no Neo4j at {uri}: start it (macOS: brew services start neo4j; Linux:"
            " sudo systemctl start neo4j); install it first if needed (brew install neo4j)"
        )
    return (
        f"Neo4j at {uri} refused the password in {store.label}. A Neo4j that has never"
        " started: stop it, run nexo setup neo4j-init (sets that password), start it."
        " One already in use: set its password to the stored one (ALTER CURRENT USER"
        " SET PASSWORD in cypher-shell), or put its password in the store"
    )


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


def _container_ports(host: Host) -> dict[int, tuple[str, str]]:
    """Host port -> (compose project, container) for the engine's running containers."""
    owners: dict[int, tuple[str, str]] = {}
    for container in parse_ps(_run(*host.cli, "ps", "--format", "json")):
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
                owners = _container_ports(host)
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


def content_finding(
    casting: Casting, load: Callable[[], Settings] = Settings
) -> Finding:
    """Which runs' questions and answers are logged (spec.telemetry.content, ADR-0009).

    Read through Settings, as Nexo will: nexo.env always carries the threshold, so only
    the process environment overrides it, while the detector and the content-capture
    switch may come from .env too. With no detector nothing is logged (fail closed).
    """
    declared = casting.spec.telemetry.content
    try:
        settings = load()
    except ValidationError as exc:
        invalid = ", ".join(sorted({str(e["loc"][0]).upper() for e in exc.errors()}))
        return Finding(
            "content", False, f"cannot tell what is logged: {invalid} invalid"
        )
    effective = settings.telemetry_content_max_sensitivity
    provider = settings.sensitive_data_provider
    if effective == "none":
        detail = "none: no question or answer is logged"
    elif provider == "none":
        detail = (
            f"{effective}, detector none (SENSITIVE_DATA_PROVIDER): runs cannot be"
            " classified, so no question or answer is logged"
        )
    else:
        detail = (
            f"{effective}, detector {provider}: questions and answers of runs at or"
            f" below {effective} are logged; the rest are withheld"
        )
    if effective != declared:
        detail += (
            " (TELEMETRY_CONTENT_MAX_SENSITIVITY in the environment overrides"
            f" spec.telemetry.content {declared})"
        )
    if settings.capture_content:
        capture = settings.otel_instrumentation_genai_capture_message_content
        detail += (
            f" (OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT={capture}"
            " logs every run)"
        )
    return Finding("content", True, detail)


def preflight(
    casting: Casting,
    lock: dict[str, Any],
    on: set[str],
    host: Host,
    store: SecretStore,
) -> list[Finding]:
    """Everything `up` needs from this machine, without starting anything."""
    native = casting.spec.target.mode == "native"
    findings = [
        secrets_finding(store, on, Path.cwd() / ".env"),
        review_finding(casting),
        content_finding(casting),
    ]
    if containers(
        casting, on
    ):  # native with no telemetry and no add-ons needs no engine
        compose_ok = bool(_run(host.engine, "compose", "version"))
        findings.insert(
            0,
            Finding(
                "engine",
                compose_ok,
                f"{host.engine} compose ({host.os})"
                + ("" if compose_ok else "; telemetry and add-ons need it"),
                advisory=native,  # native: Nexo itself runs without containers
            ),
        )
        findings[1:1] = machine_findings(casting, host, on)
    bolt = casting.spec.ports.bolt
    if "neo4j" in on and (
        neo4j := neo4j_finding(store, host, lock["name"], bolt, native=native)
    ):
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
    findings += context_findings(casting)
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
    native = casting.spec.target.mode == "native"
    budget = memory_budget(casting, lock, on)
    # What the VM holds: not the models, and in native mode not Neo4j (the host runs it).
    in_vm = {
        k: v
        for k, v in budget.items()
        if k != "models" and not (native and k == "neo4j")
    }
    containers_gb = round(sum(in_vm.values()), 1)
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
    if in_vm and (vm := vm_finding(containers_gb, host, oversize=native)):
        findings.append(vm)
    return findings


def vm_finding(
    containers_gb: float, host: Host, *, oversize: bool = False
) -> Finding | None:
    """The containers fit the Podman VM; if not, the --memory to set (whole GiB).

    oversize: also say when the VM is far larger than they need (it competes with the models).
    """
    vm = engine_memory_gb(host)
    if vm is None:
        return None
    fits = containers_gb <= vm * VM_SHARE
    detail = f"containers {containers_gb} GB of {vm:.1f} GB"
    needed = math.ceil(containers_gb / VM_SHARE) * 1024
    command = f"podman machine stop && podman machine set --memory {needed} && podman machine start"
    if not fits:
        return Finding("podman vm", False, f"{detail}; raise it: {command}")
    if (
        oversize
        and vm >= OVERSIZE_RATIO * containers_gb
        and vm - containers_gb >= OVERSIZE_GB
    ):
        return Finding(
            "podman vm",
            False,
            f"{detail}: far more than they need, and that memory is not for the models;"
            f" shrink it: {command}",
            advisory=True,
        )
    return Finding("podman vm", True, detail)


def machine_findings(casting: Casting, host: Host, on: set[str]) -> list[Finding]:
    """Which Podman machine is active against the one the setup names, and its socket."""
    name = casting.spec.target.machine
    machine = host.machine
    findings = []
    if name and machine is None:
        return [
            Finding(
                "podman machine",
                False,
                f"{name} does not exist: podman machine init --rootful {name}",
            )
        ]
    if name:
        others = [other for other in host.running if other != name]
        if machine.running:
            detail, ok = f"{name} is active, as configured", True
        elif others:
            detail, ok = (
                f"{', '.join(others)} is active, {name} is configured: up --switch-machine"
                f" stops {others[0]} (and every container on it) and starts {name}",
                False,
            )
        else:
            detail, ok = f"{name} is configured and stopped; up starts it", True
        findings.append(Finding("podman machine", ok, detail))
    if machine and "hostMetrics" in on and not machine.rootful:
        findings.append(
            Finding(
                "podman socket",
                False,
                f"hostMetrics mounts {ROOTFUL_SOCKET}, which only a rootful machine has"
                f" (rootless fails: mkdir {ROOTFUL_SOCKET}: permission denied):"
                f" podman machine stop {machine.name} && podman machine set --rootful"
                f" {machine.name} && podman machine start {machine.name}",
            )
        )
    return findings
