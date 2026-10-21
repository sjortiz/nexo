"""Podman machines: the one a setup names, the one running, and switching between them.

Podman 5.6 runs a single machine at a time, and stopping a machine stops every container on
it. Every call goes through a `Runner`, so tests inject a fake and never reach the real podman.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from nexo.setup.schema import SetupError

log = logging.getLogger(__name__)
# Where a rootful machine serves the API; a rootless one has no such socket.
ROOTFUL_SOCKET = "/run/podman/podman.sock"
LIFECYCLE_TIMEOUT_S = 300


class Ran(NamedTuple):
    """A finished command: its exit code and stdout (stderr when it failed)."""

    code: int
    out: str


Runner = Callable[[Sequence[str], int], Ran]


def run_command(command: Sequence[str], timeout: int = 30) -> Ran:
    """Run a command; a missing program or a timeout is exit code 127."""
    try:
        result = subprocess.run(
            list(command), capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return Ran(127, "")
    out = result.stdout if result.returncode == 0 else result.stderr or result.stdout
    return Ran(result.returncode, out.strip())


@dataclass(frozen=True)
class PodmanMachine:
    """What `podman machine inspect` says about one machine."""

    name: str
    running: bool
    rootful: bool
    memory_gb: float

    @property
    def connection(self) -> str:
        """The system connection for this machine's mode (a rootful one is `<name>-root`)."""
        return f"{self.name}-root" if self.rootful else self.name


class Machines:
    """The Podman machines on this computer."""

    def __init__(self, run: Runner | None = None) -> None:
        self._run = run or run_command

    def inspect(self, name: str = "") -> PodmanMachine | None:
        """The named machine (the default one when blank); None when there is none."""
        command = ["podman", "machine", "inspect", *([name] if name else [])]
        ran = self._run(command, 30)
        try:
            info = json.loads(ran.out)[0]
            return PodmanMachine(
                name=info["Name"],
                running=info["State"] == "running",
                rootful=bool(info["Rootful"]),
                memory_gb=info["Resources"]["Memory"] / 1024,
            )
        except (ValueError, LookupError, TypeError):
            return None

    def running(self) -> tuple[str, ...]:
        """Names of the machines that are running now."""
        ran = self._run(["podman", "machine", "list", "--format", "json"], 30)
        try:
            return tuple(m["Name"] for m in json.loads(ran.out) if m["Running"])
        except (ValueError, LookupError, TypeError):
            return ()

    def stop(self, name: str) -> None:
        """Stop a machine and every container on it."""
        self._lifecycle("stop", name)

    def start(self, name: str) -> None:
        """Start a machine."""
        self._lifecycle("start", name)

    def _lifecycle(self, verb: str, name: str) -> None:
        ran = self._run(["podman", "machine", verb, name], LIFECYCLE_TIMEOUT_S)
        if ran.code:
            raise SetupError(f"podman machine {verb} {name} failed: {ran.out}")


def activate(machines: Machines, name: str, *, switch: bool) -> None:
    """Make `name` the running machine; stop another only when `switch` allows it."""
    machine = machines.inspect(name)
    if machine is None:
        raise SetupError(
            f"podman machine {name} does not exist: podman machine init --rootful {name}"
        )
    if machine.running:
        return
    others = [other for other in machines.running() if other != name]
    if others and not switch:
        active = ", ".join(others)
        raise SetupError(
            f"podman machine {active} is running, but this setup uses {name}, and Podman"
            f" runs one machine at a time. Stopping {active} stops every container on it."
            f" To go ahead: nexo setup up --switch-machine (make up SWITCH=1), or"
            f" yourself: podman machine stop {active} && podman machine start {name}"
        )
    for other in others:
        log.info("stopping podman machine %s and every container on it", other)
        machines.stop(other)
    log.info("starting podman machine %s", name)
    machines.start(name)
