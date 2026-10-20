"""Preflight: what this machine has, and whether the setup fits it (foundry's gauge)."""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexo.local_profiles import MODEL_SIZES_GB, parse_profile
from nexo.setup.components import MEMORY_GB
from nexo.setup.envfile import missing_required
from nexo.setup.render import Host
from nexo.setup.schema import Casting

# Share of memory the setup may use: the rest is for the system and your apps.
MEMORY_SHARE, VM_SHARE = 0.85, 0.9
CDI_DIRS = ("/etc/cdi", "/var/run/cdi")
# Findings that stop check and up unless --force: the machine may still cope.
FORCEABLE = {"profile", "memory", "podman vm", "host", "gpu"}


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


def preflight(
    casting: Casting, lock: dict[str, Any], on: set[str], host: Host, root: Path
) -> list[Finding]:
    """Everything `up` needs from this machine, without starting anything."""
    findings = [
        Finding(
            "engine",
            bool(_run(host.engine, "compose", "version")),
            f"{host.engine} compose ({host.os})",
        )
    ]
    env_path = root / ".env"
    missing = missing_required(env_path, on)
    findings.append(
        Finding(
            "secrets",
            env_path.exists() and not missing,
            f"set in .env: {', '.join(missing)}" if missing else ".env",
        )
    )
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
