"""`nexo setup`: setup file -> lock -> rendered stack, from the real fragments in deploy/."""

from __future__ import annotations

import json
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from nexo import cli
from nexo.config import Settings
from nexo.local_profiles import plan
from nexo.setup import check
from nexo.setup.check import Finding, memory_budget
from nexo.setup import commands
from nexo.setup.commands import model_lines
from nexo.setup.components import COMPONENTS, MEMORY_GB, default_images
from nexo.setup.envfile import ensure_secrets, read_env
from nexo.setup.lock import lock
from nexo.setup.render import (
    PORTABLE_PRESET,
    RENDER_DIR,
    Host,
    build_env,
    portable_files,
    render,
)
from nexo.setup.schema import SetupError, load_casting
from nexo.setup.status import assess, stale_volume_hint

ROOT = Path(__file__).resolve().parent.parent
PRESETS = ROOT / "examples/setups"
MAC = Host(os="macos", engine="podman", socket="/run/podman.sock", hostname="studio")
LINUX = Host(os="linux", engine="docker", socket="/var/run/docker.sock", hostname="box")
STACK = {
    "neo4j",
    "otel-collector",
    "nexo",
    "signoz",
    "signoz-ingester",
    "signoz-clickhouse",
}


@pytest.fixture(name="workdir")
def _workdir(tmp_path: Path) -> Path:
    """A checkout-like folder: the repo's deploy/ fragments, its own .nexo and .env."""
    (tmp_path / "deploy").symlink_to(ROOT / "deploy")
    return tmp_path


def _setup(workdir: Path, preset: str, edit=None) -> Path:
    data = yaml.safe_load((PRESETS / preset / "nexo.yaml").read_text())
    if edit:
        edit(data)
    path = workdir / "nexo.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def _render(workdir: Path, path: Path, host: Host = MAC, addons=frozenset()):
    casting = load_casting(path)
    out = render(
        casting, lock(casting, path, workdir), workdir, host, frozenset(addons)
    )
    return (
        yaml.safe_load((out / "compose.yaml").read_text()),
        yaml.safe_load((out / "otel-collector.yaml").read_text()),
        read_env(out / "nexo.env"),
    )


@pytest.mark.parametrize(
    "preset, services, env",
    [
        ("mac-single", set(), {"LOCAL_PROFILE": "m5-pro-24"}),
        (
            "mac-two-machines",
            set(),
            {
                "REMOTE_PROFILE": "m1-max-64",
                "REMOTE_BASE_URL": "http://localhost:11435/v1",
            },
        ),
        ("linux-cpu", set(), {"LOCAL_PROFILE": "cpu-64"}),
        ("linux-gpu", set(), {"LOCAL_PROFILE": "gpu-24"}),
        (
            "linux-vllm",
            {"vllm-inference-service", "vllm-embedding-service"},
            {
                "VLLM_BASE_URL": "http://localhost:8000/v1",
                "EMBEDDING_MODEL": "BAAI/bge-m3",
            },
        ),
    ],
)
def test_each_preset_renders_its_stack_and_settings(workdir, preset, services, env):
    """Core stack plus the preset's model servers; nexo.env carries its model settings."""
    compose, collector, rendered_env = _render(workdir, _setup(workdir, preset))
    names = set(compose["services"])
    assert STACK | services <= names
    assert not {"langfuse-web", "jaeger"} & names  # add-ons stay off
    assert ("vllm-inference-service" in names) == bool(services)
    assert env.items() <= rendered_env.items()
    assert rendered_env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:4318"
    assert set(collector["service"]["pipelines"]) == {
        "traces",
        "metrics",
        "logs",
        "metrics/infra",
    }
    assert compose["name"] == "nexo"


def test_an_addon_brings_its_services_pipeline_and_required_keys(workdir):
    """--addon langfuse (make up LANGFUSE=1) renders Langfuse; without it nothing of it remains."""
    path = _setup(workdir, "mac-single")
    compose, collector, _ = _render(workdir, path, addons={"langfuse"})
    assert {"langfuse-web", "langfuse-worker"} <= set(compose["services"])
    assert "traces/langfuse" in collector["service"]["pipelines"]
    assert collector["service"]["extensions"] == ["basicauth/langfuse"]
    collector_env = compose["services"]["otel-collector"]["environment"]
    assert collector_env["LANGFUSE_SECRET_KEY"].startswith("${LANGFUSE_SECRET_KEY:?")

    compose, collector, _ = _render(workdir, path)
    assert not any(name.startswith("langfuse") for name in compose["services"])
    assert "traces/langfuse" not in collector["service"]["pipelines"]
    assert "LANGFUSE" not in yaml.safe_dump(compose)


def test_host_decides_how_the_nexo_container_reaches_host_services(workdir):
    """macOS goes through the engine's gateway; Linux shares the host network."""
    path = _setup(workdir, "mac-two-machines")
    compose, _, _ = _render(workdir, path, MAC)
    app = compose["services"]["nexo"]
    assert (
        app["environment"]["REMOTE_BASE_URL"]
        == "http://host.containers.internal:11435/v1"
    )
    assert "network_mode" not in app
    compose, _, _ = _render(workdir, path, LINUX)
    assert compose["services"]["nexo"]["network_mode"] == "host"
    assert "environment" not in compose["services"]["nexo"]


def test_lock_keeps_pins_until_upgrade(workdir):
    """Editing nexo.yaml keeps earlier pins; the file's image wins; --upgrade resets."""
    path = _setup(workdir, "mac-single")
    casting = load_casting(path)
    first = lock(casting, path, workdir)
    default = default_images(ROOT, "neo4j")["neo4j"]
    assert first["images"]["neo4j"]["neo4j"] == default
    lock_path = workdir / "nexo.lock"
    lock_path.write_text(lock_path.read_text().replace(default, "neo4j:5.0-old"))

    edited = _setup(
        workdir,
        "mac-single",
        lambda d: d["spec"]["addons"].update(jaeger={"enabled": True}),
    )
    second = lock(load_casting(edited), edited, workdir)
    assert second["enabled"]["jaeger"] is True
    assert second["images"]["neo4j"]["neo4j"] == "neo4j:5.0-old"

    pinned = _setup(
        workdir,
        "mac-single",
        lambda d: d["spec"].update(components={"neo4j": {"image": "neo4j:6"}}),
    )
    assert (
        lock(load_casting(pinned), pinned, workdir)["images"]["neo4j"]["neo4j"]
        == "neo4j:6"
    )
    reset = _setup(workdir, "mac-single")
    upgraded = lock(load_casting(reset), reset, workdir, upgrade=True)
    assert upgraded["images"]["neo4j"]["neo4j"] == default


@pytest.mark.parametrize(
    "edit, message",
    [
        (lambda d: d["spec"]["addons"].update(grafana={"enabled": True}), "grafana"),
        (lambda d: d["spec"]["models"]["local"].update(profile="m1-huge"), "m1-huge"),
        (lambda d: d["spec"]["models"].pop("local"), "models.local"),
        (
            lambda d: d["spec"].update(components={"signoz": {"enabled": False}}),
            "cannot be disabled",
        ),
        (lambda d: d.update(apiVersion="v1"), "apiVersion"),
    ],
)
def test_invalid_setup_files_are_rejected_naming_the_problem(workdir, edit, message):
    """A typo or impossible combination stops before anything renders."""
    with pytest.raises(SetupError, match=message):
        load_casting(_setup(workdir, "mac-single", edit))


def test_patches_change_rendered_files_and_bad_ones_fail(workdir):
    """RFC 6902 operations apply last; a failing `test` or unknown target is an error."""
    limit = {"op": "add", "path": "/services/neo4j/mem_limit", "value": "2g"}
    path = _setup(
        workdir,
        "mac-single",
        lambda d: d["spec"].update(
            patches=[{"target": "compose.yaml", "operations": [limit]}]
        ),
    )
    compose, _, _ = _render(workdir, path)
    assert compose["services"]["neo4j"]["mem_limit"] == "2g"

    for patch in (
        {
            "target": "compose.yaml",
            "operations": [{"op": "test", "path": "/name", "value": "x"}],
        },
        {"target": "nexo.env", "operations": []},
    ):
        bad = _setup(
            workdir, "mac-single", lambda d, p=patch: d["spec"].update(patches=[p])
        )
        with pytest.raises(SetupError, match="patch"):
            _render(workdir, bad)


def test_collector_config_override_is_validated(workdir):
    """components.collector.config merges in; a pipeline naming a missing exporter fails."""
    broken = {"service": {"pipelines": {"traces": {"exporters": ["otlp/nowhere"]}}}}
    path = _setup(
        workdir,
        "mac-single",
        lambda d: d["spec"].update(components={"collector": {"config": broken}}),
    )
    with pytest.raises(SetupError, match="otlp/nowhere"):
        _render(workdir, path)


def test_secrets_are_generated_for_enabled_components_only_and_kept(workdir):
    """make env: fills empty keys, keeps existing values, never writes disabled add-ons' keys."""
    env = workdir / ".env"
    env.write_text("NEO4J_PASSWORD=keep-me\nSIGNOZ_JWT_SECRET=\n")
    generated = ensure_secrets(env, workdir, {"neo4j", "signoz"})
    values = read_env(env)
    assert values["NEO4J_PASSWORD"] == "keep-me"
    assert set(generated) == set(COMPONENTS["signoz"].secrets)
    assert all(values[key] for key in COMPONENTS["signoz"].secrets)
    assert not any(key.startswith("LANGFUSE") for key in values)
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert not ensure_secrets(env, workdir, {"neo4j", "signoz"})


def test_every_required_variable_in_a_fragment_is_generated_or_declared():
    """A ${VAR:?} the stack needs is either generated by make env or listed as user-provided."""
    declared = {k for c in COMPONENTS.values() for k in (*c.secrets, *c.required)}
    for component in COMPONENTS.values():
        if component.compose:
            text = (ROOT / component.compose).read_text()
            required = set(re.findall(r"\$\{(\w+):\?", text))
            assert required <= declared, (component.name, required - declared)


def test_memory_budget_counts_only_models_this_machine_serves(workdir):
    """Two machines: the local Mac pays for the light models, not the remote's agent model."""
    budgets = {}
    for preset in ("mac-single", "mac-two-machines"):
        path = _setup(workdir, preset)
        casting = load_casting(path)
        locked = lock(casting, path, workdir)
        on = {name for name, enabled in locked["enabled"].items() if enabled}
        budgets[preset] = memory_budget(casting, locked, on)
    assert budgets["mac-single"]["neo4j"] == MEMORY_GB["neo4j"]
    assert budgets["mac-single"]["models"] == plan("m5-pro-24").weights_gb
    # The remote Mac's agent and extraction models are not counted here.
    assert (
        budgets["mac-two-machines"]["models"] == plan("m5-pro-24", "light").weights_gb
    )


def test_settings_read_the_rendered_env_after_dot_env(workdir, monkeypatch):
    """nexo.yaml wins over .env; a real environment variable wins over both."""
    monkeypatch.chdir(workdir)
    for key in ("LOCAL_PROFILE", "AGENT_MODEL"):
        monkeypatch.delenv(key, raising=False)
    (workdir / ".env").write_text("LOCAL_PROFILE=m1-max-64\nAGENT_MODEL=from-dot-env\n")
    (workdir / RENDER_DIR).mkdir(parents=True)
    (workdir / RENDER_DIR / "nexo.env").write_text("LOCAL_PROFILE=m5-pro-24\n")
    settings = Settings()
    assert settings.local_profile == "m5-pro-24"
    assert settings.agent_model == "from-dot-env"
    monkeypatch.setenv("LOCAL_PROFILE", "m1-pro-16")
    assert Settings().local_profile == "m1-pro-16"


ENGINE = next((e for e in ("docker", "podman") if shutil.which(e)), None)


def _compose_accepts(compose_file: Path, env_dir: Path) -> None:
    """The engine's compose validates the file, with a value for every key the stack reads."""
    keys = {k for c in COMPONENTS.values() for k in (*c.secrets, *c.required)}
    env = env_dir / ".env"
    env.write_text("".join(f"{k}=x\n" for k in sorted(keys)))
    command = [ENGINE, "compose", "-f", str(compose_file), "--env-file", str(env)]
    result = subprocess.run(
        command + ["config", "-q"],
        check=False,
        timeout=60,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        pytest.fail(result.stderr)


@pytest.mark.skipif(ENGINE is None, reason="no container engine")
@pytest.mark.parametrize("preset", sorted(p.name for p in PRESETS.iterdir()))
def test_rendered_compose_files_are_valid_compose(workdir, preset):
    """The engine's own compose accepts every preset's render (with every add-on on)."""
    path = _setup(workdir, preset)
    _render(workdir, path, addons={"langfuse", "jaeger", "langsmith"})
    _compose_accepts(workdir / RENDER_DIR / "compose.yaml", workdir)


def test_committed_compose_file_is_current(tmp_path):
    """docker-compose.yml and deploy/rendered/ match `make compose-file`; plain compose takes it."""
    files = portable_files(load_casting(ROOT / PORTABLE_PRESET), ROOT)
    for relative, text in files.items():
        stale = f"{relative} is stale: make compose-file"
        assert (ROOT / relative).read_text() == text, stale
    committed = (ROOT / "docker-compose.yml").read_text()
    assert str(ROOT) not in committed
    assert "${CONTAINER_SOCKET:-/var/run/docker.sock}" in committed
    if ENGINE is not None:
        _compose_accepts(ROOT / "docker-compose.yml", tmp_path)


def test_model_lines_report_off_roles_and_what_a_server_has():
    """A role that is off is ok (no KeyError); a failure lists the server's models."""
    rows = [
        {
            "role": "agent",
            "model": "qwen3.5:9b",
            "base_url": "http://x/v1",
            "check": "ok",
        },
        {
            "role": "judge",
            "model": None,
            "note": "off: nexo eval skips generation scores",
        },
        {
            "role": "router",
            "model": "qwen3:4b",
            "base_url": "http://x/v1",
            "check": "model not served here",
            "served": ["bge-m3", "qwen3.5:9b"],
        },
    ]
    lines = model_lines(rows)
    assert lines[0] == (True, "model agent: qwen3.5:9b at http://x/v1")
    assert lines[1] == (True, "model judge: off: nexo eval skips generation scores")
    ok, text = lines[2]
    assert not ok
    assert "model not served here" in text and "bge-m3, qwen3.5:9b" in text


def _recording_run(seen: list[list[str]], stdout=lambda _command: ""):
    """A stand-in for subprocess.run: records each command, succeeds, prints stdout(command)."""

    def run(command, **_kwargs):
        seen.append(list(command))
        return subprocess.CompletedProcess(
            command, 0, stdout=stdout(command), stderr=""
        )

    return run


@pytest.fixture(name="fake_up")
def _fake_up(workdir, monkeypatch):
    """`nexo setup up` in workdir with the engine, preflight and model start faked."""
    seen: list[list[str]] = []
    output: dict[str, str] = {}  # compose verb -> what the fake engine prints
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(
        commands.subprocess,
        "run",
        _recording_run(
            seen, lambda c: next((v for k, v in output.items() if k in c), "")
        ),
    )
    monkeypatch.setattr(commands.time, "sleep", lambda _s: None)
    monkeypatch.setattr(commands, "detect_host", lambda _casting: MAC)
    findings: list[Finding] = []
    monkeypatch.setattr(commands, "preflight", lambda *_args: findings)
    _setup(workdir, "mac-single")

    def up(*addons: str, force: bool = False) -> int:
        argv = ["setup", "up", "--skip-models", *(f"--addon={a}" for a in addons)]
        return cli.main(argv + (["--force"] if force else []))

    up.output = output
    return up, seen, findings


def test_a_plain_up_removes_add_ons_a_previous_run_enabled(workdir, fake_up):
    """make up LANGFUSE=1, then make up: Langfuse is gone from the render and its containers go."""
    up, seen, _ = fake_up
    assert up("langfuse") == 0
    assert "langfuse-web" in (workdir / RENDER_DIR / "compose.yaml").read_text()
    assert up() == 0
    assert "langfuse-web" not in (workdir / RENDER_DIR / "compose.yaml").read_text()
    compose_up = [c for c in seen if c[1:2] == ["compose"] and "up" in c]
    assert len(compose_up) == 2
    assert all("--remove-orphans" in c for c in compose_up)


def _memory(workdir, monkeypatch, preset, sizes, addons=()):
    """memory_findings for a preset; sizes = (machine GiB, Podman VM GiB or None)."""
    machine_gb, vm_gb = sizes
    monkeypatch.setattr(check, "machine_memory_gb", lambda: machine_gb)
    monkeypatch.setattr(check, "engine_memory_gb", lambda _host: vm_gb)
    path = _setup(workdir, preset)
    casting = load_casting(path)
    locked = lock(casting, path, workdir)
    on = {n for n, enabled in locked["enabled"].items() if enabled} | set(addons)
    return {f.name: f for f in check.memory_findings(casting, locked, on, MAC)}


def test_memory_is_checked_against_the_real_machine_and_vm(workdir, monkeypatch):
    """A 64 GB profile on a 24 GB machine fails; so do add-ons beyond the VM or the machine."""
    fits = _memory(workdir, monkeypatch, "mac-single", (24.0, 8.0))
    assert all(f.ok for f in fits.values()), fits
    wrong = _memory(workdir, monkeypatch, "linux-cpu", (24.0, None))
    assert not wrong["profile"].ok and "cpu-64 is for 64 GB" in wrong["profile"].detail
    assert not wrong["memory"].ok  # 64 GB tier models on 24 GB
    vm = _memory(
        workdir, monkeypatch, "mac-single", (24.0, 4.0), ("langfuse", "jaeger")
    )
    assert not vm["podman vm"].ok and "of 4.0 GB" in vm["podman vm"].detail
    small = _memory(workdir, monkeypatch, "mac-single", (12.0, None), ("langfuse",))
    assert not small["memory"].ok


@pytest.mark.parametrize(
    "failed, force, blocked",
    [
        ("memory", False, True),
        ("memory", True, False),
        ("podman vm", True, False),
        ("engine", True, True),  # --force never overrides a missing engine or secret
    ],
)
def test_memory_failures_block_up_unless_forced(fake_up, failed, force, blocked):
    """check and up stop on a failed memory check; --force (FORCE=1) lets them go ahead."""
    up, seen, findings = fake_up
    findings.append(Finding(failed, False, "too much"))
    assert up(force=force) == (1 if blocked else 0)
    started = [c for c in seen if c[1:2] == ["compose"] and "up" in c]
    assert bool(started) is not blocked


def test_rendered_env_overrides_stale_profiles_in_dot_env(workdir, tmp_path):
    """A LOCAL_PROFILE left in .env does not send vLLM's roles to Ollama."""
    stale = tmp_path / "stale.env"
    stale.write_text("LOCAL_PROFILE=m5-pro-24\nREMOTE_PROFILE=m1-max-64\n")
    for preset, agent_url in (
        ("linux-vllm", "http://localhost:8000/v1"),
        ("mac-single", "http://localhost:11434/v1"),
    ):
        rendered = tmp_path / f"{preset}.env"
        env = build_env(load_casting(_setup(workdir, preset)))
        rendered.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
        settings = Settings(_env_file=(stale, rendered))
        assert settings.remote_profile is None, preset
        assert settings.endpoint("agent").base_url == agent_url, preset


@pytest.mark.parametrize(
    "preset, machine, expected",
    [
        ("linux-cpu", ("macos", set()), {"host": False}),
        ("linux-cpu", ("linux", set()), {"host": True}),
        ("mac-single", ("macos", set()), {"host": True}),
        ("linux-gpu", ("linux", set()), {"host": True, "gpu": False}),
        ("linux-gpu", ("linux", {"rocm-smi"}), {"host": True, "gpu": True}),
        ("linux-vllm", ("linux", set()), {"host": True, "gpu": False}),
        ("linux-vllm", ("linux", {"nvidia-smi"}), {"host": True, "gpu": True}),
    ],
)
def test_platform_findings(workdir, monkeypatch, preset, machine, expected):
    """Linux presets on a Mac fail the host check; GPU presets need their GPU tooling."""
    machine, tools = machine
    monkeypatch.setattr(check, "this_os", lambda: machine)
    monkeypatch.setattr(
        check.shutil, "which", lambda name: name if name in tools else None
    )
    monkeypatch.setattr(check, "_run", lambda *_cmd: '{"nvidia":{}}')  # docker runtime
    casting = load_casting(_setup(workdir, preset))
    pinned = casting.spec.target.host
    host = Host(
        os=machine if pinned == "auto" else pinned,
        engine="docker",
        socket="/var/run/docker.sock",
        hostname="box",
    )
    found = {f.name: f.ok for f in check.platform_findings(casting, host)}
    assert found == expected


def test_linux_presets_render_host_networking_even_on_a_mac(workdir, monkeypatch):
    """target.host is pinned in the Linux presets, so the render does not depend on the Mac."""
    for preset in ("linux-cpu", "linux-gpu", "linux-vllm"):
        assert load_casting(_setup(workdir, preset)).spec.target.host == "linux"
    monkeypatch.setattr(check, "this_os", lambda: "macos")
    compose, _, _ = _render(workdir, _setup(workdir, "linux-cpu"), LINUX)
    assert compose["services"]["nexo"]["network_mode"] == "host"


def test_check_names_the_models_up_will_pull(workdir):
    """Two machines serve another light set than one machine; check says what is missing."""
    single = load_casting(_setup(workdir, "mac-single"))
    single_lock = lock(single, workdir / "nexo.yaml", workdir)
    have = {"qwen3.5:9b-q4_K_M", "bge-m3:latest"}
    found = check.models_finding(single, single_lock, pulled=lambda _port: have)
    assert found.ok and "up will pull" not in found.detail

    two = load_casting(_setup(workdir, "mac-two-machines"))
    found = check.models_finding(
        two, lock(two, workdir / "nexo.yaml", workdir), pulled=lambda _port: have
    )
    to_pull = found.detail.split("up will pull ")[1]
    assert "qwen3:4b-instruct-2507-q4_K_M (2.5 GB)" in to_pull
    assert "llama3.1:8b-instruct-q4_K_M (4.9 GB)" in to_pull
    assert "bge-m3" not in to_pull  # pulled as bge-m3:latest

    down = check.models_finding(single, single_lock, pulled=lambda _port: None)
    assert "Ollama is not running" in down.detail
    vllm = load_casting(_setup(workdir, "linux-vllm"))
    assert (
        check.models_finding(vllm, lock(vllm, workdir / "nexo.yaml", workdir)) is None
    )


SIGNOZ_FAILED = "\n".join(
    json.dumps(c)
    for c in (
        {"Service": "neo4j", "State": "running", "Health": "healthy", "ExitCode": 0},
        {"Service": "signoz-migrator", "State": "exited", "Health": "", "ExitCode": 0},
        {"Service": "signoz", "State": "exited", "Health": "", "ExitCode": 1},
    )
)


def test_up_fails_when_a_service_exits_and_names_a_stale_volume(fake_up, capfd):
    """SigNoz refused by Postgres (an older .env made the volume): up returns 1 with the fix."""
    up, _, _ = fake_up
    up.output.update(ps=SIGNOZ_FAILED, logs="FATAL: failed SASL auth (password)")
    assert up() == 1
    captured = capfd.readouterr()
    errors = captured.out + captured.err
    assert "signoz did not start" in errors
    assert "volume rm nexo_signoz-postgres-data" in errors
    assert "clickhouse" not in errors.split("volume rm")[1]


def test_up_succeeds_when_one_shot_jobs_exit_zero(fake_up):
    """Exited migrators are done, not failed; an unrelated failure gives no volume hint."""
    up, _, _ = fake_up
    up.output["ps"] = "[" + ",".join(SIGNOZ_FAILED.splitlines()[:2]) + "]"  # array form
    assert up() == 0


@pytest.mark.parametrize(
    "containers, expected",
    [
        ([{"Service": "a", "State": "running", "Health": "starting"}], ([], ["a"])),
        ([{"Service": "a", "State": "restarting"}], (["a"], [])),
        ([{"Service": "a", "State": "running", "Health": "unhealthy"}], (["a"], [])),
        ([{"Service": "a", "State": "exited", "ExitCode": 0}], ([], [])),
        ([{"Service": "a", "State": "running", "Health": ""}], ([], [])),
    ],
)
def test_assess_container_states(containers, expected):
    """Starting waits; restarting, unhealthy or a non-zero exit fails; exit 0 is done."""
    assert assess(containers) == expected


def test_no_volume_hint_without_an_auth_failure():
    """Other crashes get their logs, not advice to delete data."""
    compose = {"name": "nexo", "services": {"signoz": {}}, "volumes": {}}
    assert (
        stale_volume_hint("signoz", "panic: out of memory", compose, "podman") is None
    )
