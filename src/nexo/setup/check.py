"""Preflight: what this machine has, and whether the setup fits it (foundry's gauge)."""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexo.local_profiles import MODEL_SIZES_GB, parse_profile
from nexo.setup.components import MEMORY_GB
from nexo.setup.envfile import missing_required
from nexo.setup.render import Host
from nexo.setup.schema import Casting, SetupError


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
    """This machine's physical memory."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
    except (ValueError, OSError, AttributeError):
        return None


def engine_memory_gb(host: Host) -> float | None:
    """The Podman VM's memory limit on macOS (containers cannot use more)."""
    if host.engine != "podman" or host.os != "macos":
        return None
    mib = _run("podman", "machine", "inspect", "--format", "{{.Resources.Memory}}")
    return int(mib.splitlines()[0]) * 1.048576 / 1000 if mib else None


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
        findings.append(
            Finding(
                "ollama",
                bool(shutil.which("ollama")),
                "brew install ollama, or ollama.com",
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
    findings += _memory_findings(casting, lock, on, host)
    return findings


def _memory_findings(
    casting: Casting, lock: dict[str, Any], on: set[str], host: Host
) -> list[Finding]:
    budget = memory_budget(casting, lock, on)
    containers = round(sum(v for k, v in budget.items() if k != "models"), 1)
    total = round(sum(budget.values()), 1)
    models = casting.spec.models
    # The profile names this machine's memory; otherwise ask the OS.
    machine = (
        parse_profile(models.local.profile).memory_gb
        if models.local is not None
        else machine_memory_gb()
    )
    parts = ", ".join(f"{k} {v}" for k, v in budget.items())
    findings = [
        Finding(
            "memory",
            machine is None or total <= machine * 0.85,
            (
                f"{total} GB of {machine:.0f} GB ({parts})"
                if machine
                else f"{total} GB ({parts})"
            ),
        )
    ]
    vm = engine_memory_gb(host)
    if vm is not None:
        findings.append(
            Finding(
                "podman vm",
                containers <= vm * 0.9,
                f"containers {containers} GB of {vm:.1f} GB (podman machine set --memory to raise)",
            )
        )
    return findings


def require(findings: list[Finding]) -> None:
    """Raise with every failed finding."""
    failed = [f"{f.name}: {f.detail}" for f in findings if not f.ok]
    if failed:
        raise SetupError("preflight failed - " + "; ".join(failed))
