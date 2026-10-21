"""Podman machines: the setup's machine, switching, rootless detection and VM size (all faked)."""

from __future__ import annotations

import json
import subprocess

import pytest

from nexo import cli
from nexo.setup import check, commands, engine
from nexo.setup.check import detect_host, machine_findings, vm_finding
from nexo.setup.engine import Machines, Ran, activate
from nexo.setup.lock import lock
from nexo.setup.render import Host
from nexo.setup.schema import SetupError, load_casting
from tests.test_setup import MAC, _setup


def _spec(**fields):
    """An edit for _setup: set these fields under spec."""
    return lambda data: data["spec"].update(fields)


def _target(**fields):
    """An edit for _setup: set these fields under spec.target."""
    return lambda data: data["spec"]["target"].update(fields)


class FakePodman:
    """`podman machine ...` over a dict of machines; records every command it is given."""

    def __init__(self, **machines: dict):
        self.machines = {
            name: {"running": False, "rootful": True, "mib": 3072} | state
            for name, state in machines.items()
        }
        self.calls: list[list[str]] = []

    def __call__(self, command, timeout=30):  # pylint: disable=unused-argument
        command = list(command)
        self.calls.append(command)
        if command[:3] == ["podman", "machine", "inspect"]:
            name = command[3] if len(command) > 3 else next(iter(self.machines))
            state = self.machines.get(name)
            if state is None:
                return Ran(125, "no such machine")
            return Ran(
                0,
                json.dumps(
                    [
                        {
                            "Name": name,
                            "State": "running" if state["running"] else "stopped",
                            "Rootful": state["rootful"],
                            "Resources": {"Memory": state["mib"]},
                        }
                    ]
                ),
            )
        if command[:3] == ["podman", "machine", "list"]:
            rows = [
                {"Name": n, "Running": m["running"]} for n, m in self.machines.items()
            ]
            return Ran(0, json.dumps(rows))
        if command[:3] in (
            ["podman", "machine", "start"],
            ["podman", "machine", "stop"],
        ):
            self.machines[command[3]]["running"] = command[2] == "start"
        return Ran(0, "")

    @property
    def lifecycle(self) -> list[str]:
        """The start and stop commands, in order, as `verb name`."""
        return [" ".join(c[2:]) for c in self.calls if c[2:3] in (["start"], ["stop"])]


def test_the_suite_never_reaches_the_real_podman():
    """conftest's guard fails a test that would run podman or docker."""
    with pytest.raises(AssertionError, match="real podman call"):
        engine.run_command(["podman", "ps"])


def test_a_machine_needs_podman_not_docker(workdir):
    """target.machine with engine docker names a machine that cannot exist."""
    edit = _target(engine="docker", machine="m")
    with pytest.raises(SetupError, match="engine docker has none"):
        load_casting(_setup(workdir, "mac-native", edit))


def test_activate_leaves_a_running_machine_alone():
    """A machine that already runs is not touched."""
    fake = FakePodman(**{"nexo-telemetry": {"running": True}})
    activate(Machines(fake), "nexo-telemetry", switch=False)
    assert fake.lifecycle == []


def test_activate_starts_the_machine_when_none_is_running():
    """With nothing running, up just starts the named machine."""
    fake = FakePodman(**{"nexo-telemetry": {}})
    activate(Machines(fake), "nexo-telemetry", switch=False)
    assert fake.lifecycle == ["start nexo-telemetry"]


def test_activate_refuses_to_stop_another_machine_without_the_flag():
    """Another machine is running and no flag: nothing stops, and the message says why."""
    fake = FakePodman(
        **{"nexo-telemetry": {}, "podman-machine-default": {"running": True}}
    )
    with pytest.raises(SetupError) as raised:
        activate(Machines(fake), "nexo-telemetry", switch=False)
    message = str(raised.value)
    assert "podman-machine-default is running" in message
    assert "stops every container on it" in message and "--switch-machine" in message
    assert fake.lifecycle == []


def test_activate_switches_machines_when_asked():
    """--switch-machine stops the other machine first, then starts the named one."""
    fake = FakePodman(
        **{"nexo-telemetry": {}, "podman-machine-default": {"running": True}}
    )
    activate(Machines(fake), "nexo-telemetry", switch=True)
    assert fake.lifecycle == ["stop podman-machine-default", "start nexo-telemetry"]
    assert Machines(fake).running() == ("nexo-telemetry",)


def test_activate_names_a_machine_that_does_not_exist():
    """A typo in target.machine is named, with the command to create it."""
    with pytest.raises(SetupError, match="ghost does not exist"):
        activate(Machines(FakePodman()), "ghost", switch=True)


def test_a_failing_stop_is_a_setup_error():
    """A podman machine command that fails reports its error instead of going on."""

    def run(command, timeout=30):  # pylint: disable=unused-argument
        return Ran(125, "boom")

    with pytest.raises(SetupError, match="stop m failed: boom"):
        Machines(run).stop("m")


@pytest.fixture(name="up_with_machine")
def _up_with_machine(workdir, monkeypatch):
    """mac-native naming the nexo-telemetry machine, with podman and compose faked."""
    fake = FakePodman(
        **{"nexo-telemetry": {}, "podman-machine-default": {"running": True}}
    )
    compose: list[list[str]] = []

    def run(command, **_kwargs):
        compose.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.chdir(workdir)
    monkeypatch.setattr(engine, "run_command", fake)
    monkeypatch.setattr(commands.subprocess, "run", run)
    monkeypatch.setattr(commands, "preflight", lambda *_args: [])
    monkeypatch.setattr(commands.time, "sleep", lambda _s: None)

    def edit(data):
        data["spec"]["target"] |= {"machine": "nexo-telemetry", "engine": "podman"}

    _setup(workdir, "mac-native", edit)
    return fake, compose


def test_up_fails_when_another_machine_is_active(up_with_machine):
    """No flag: nothing is stopped, rendered or started, and the error says what to do."""
    fake, compose = up_with_machine
    assert cli.main(["setup", "up", "--skip-models"]) == 2
    assert fake.lifecycle == [] and compose == []


def test_up_switches_machine_and_pins_compose_to_its_connection(up_with_machine):
    """--switch-machine stops the active machine, starts the named one, uses `<name>-root`."""
    fake, compose = up_with_machine
    assert cli.main(["setup", "up", "--skip-models", "--switch-machine"]) == 0
    assert fake.lifecycle == ["stop podman-machine-default", "start nexo-telemetry"]
    up = next(c for c in compose if "up" in c)
    assert up[:4] == ["podman", "--connection", "nexo-telemetry-root", "compose"]


def test_down_does_nothing_while_its_machine_is_stopped(up_with_machine):
    """Its containers went down with the machine; down never starts or switches machines."""
    fake, compose = up_with_machine
    assert cli.main(["setup", "down"]) == 0
    assert compose == [] and fake.lifecycle == []


def test_detect_host_pins_a_rootful_machine_and_its_socket(workdir):
    """A named rootful machine: its -root connection, its socket and its memory."""
    path = _setup(
        workdir,
        "mac-native",
        lambda d: d["spec"]["target"].update(machine="nexo-telemetry", engine="podman"),
    )
    fake = FakePodman(**{"nexo-telemetry": {"running": True, "mib": 3072}})
    host = detect_host(load_casting(path), Machines(fake))
    assert host.connection == "nexo-telemetry-root"
    assert host.cli == ["podman", "--connection", "nexo-telemetry-root"]
    assert host.socket == "/run/podman/podman.sock"
    assert check.engine_memory_gb(host) == 3.0


def _setup_with(workdir, monkeypatch, machines, addons=None):
    monkeypatch.chdir(workdir)
    path = _setup(
        workdir,
        "mac-native",
        lambda d: (
            d["spec"]["target"].update(machine="nexo-telemetry", engine="podman"),
            addons and d["spec"].update(addons=addons),
        ),
    )
    casting = load_casting(path)
    locked = lock(casting, path, workdir)
    on = {n for n, enabled in locked["enabled"].items() if enabled}
    host = detect_host(casting, Machines(FakePodman(**machines)))
    return casting, host, on


def test_check_reports_the_active_machine_against_the_configured_one(
    workdir, monkeypatch
):
    """Another machine active: a FAIL naming both and the switch; the right one: ok."""
    casting, host, on = _setup_with(
        workdir,
        monkeypatch,
        {"nexo-telemetry": {}, "podman-machine-default": {"running": True}},
    )
    (finding,) = machine_findings(casting, host, on)
    assert not finding.ok and finding.name == "podman machine"
    assert (
        "podman-machine-default is active, nexo-telemetry is configured"
        in finding.detail
    )
    assert "stops podman-machine-default (and every container on it)" in finding.detail

    casting, host, on = _setup_with(
        workdir, monkeypatch, {"nexo-telemetry": {"running": True}}
    )
    assert machine_findings(casting, host, on)[0].ok


def test_check_flags_a_rootless_machine_when_host_metrics_mounts_the_socket(
    workdir, monkeypatch
):
    """The fix is printed: only a rootful machine has /run/podman/podman.sock."""
    addons = {"hostMetrics": {"enabled": True}}
    machines = {"nexo-telemetry": {"running": True, "rootful": False}}
    casting, host, on = _setup_with(workdir, monkeypatch, machines, addons)
    socket = next(
        f for f in machine_findings(casting, host, on) if f.name == "podman socket"
    )
    assert not socket.ok
    assert (
        "podman machine stop nexo-telemetry && podman machine set --rootful"
        " nexo-telemetry && podman machine start nexo-telemetry" in socket.detail
    )
    # Rootless is fine without host metrics, and a rootful machine is fine with it.
    casting, host, on = _setup_with(workdir, monkeypatch, machines)
    assert [f.name for f in machine_findings(casting, host, on)] == ["podman machine"]
    casting, host, on = _setup_with(
        workdir, monkeypatch, {"nexo-telemetry": {"running": True}}, addons
    )
    assert [f.name for f in machine_findings(casting, host, on)] == ["podman machine"]


def test_check_names_a_machine_that_does_not_exist(workdir, monkeypatch):
    """check says how to create a machine the setup names but podman lacks."""
    casting, host, on = _setup_with(workdir, monkeypatch, {"other": {}})
    (finding,) = machine_findings(casting, host, on)
    assert (
        not finding.ok
        and "podman machine init --rootful nexo-telemetry" in finding.detail
    )


@pytest.mark.parametrize(
    "mib, ok, advisory",
    [(3072, True, False), (12288, False, True), (1024, False, False)],
)
def test_vm_finding_warns_on_a_machine_far_above_the_containers(mib, ok, advisory):
    """Collector and SigNoz need ~1.5 GB: 3 GiB is right, 12 GiB competes with the models."""
    machine = engine.PodmanMachine("m", True, True, mib / 1024)
    host = Host("macos", "podman", "", "h", machine=machine)
    finding = vm_finding(1.5, host, oversize=True)
    assert (finding.ok, finding.advisory) == (ok, advisory)
    assert vm_finding(1.5, host).ok == (
        mib != 1024
    )  # without oversize: only a fit counts


def test_compose_mode_is_unchanged_without_a_machine(workdir, monkeypatch):
    """No target.machine: no machine finding, and compose is not pinned to a connection."""
    monkeypatch.chdir(workdir)
    casting = load_casting(_setup(workdir, "mac-single"))
    assert not machine_findings(casting, MAC, {"neo4j"})
    assert MAC.cli == ["podman"]
