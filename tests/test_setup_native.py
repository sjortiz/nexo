"""target.mode native, profile detection, `nexo config --explain` and setup preflight details."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from nexo import cli, config
from nexo.config import Settings, explain, render_drift
from nexo.secret_store import store_from_options
from nexo.setup import check, commands
from nexo.setup.check import Finding, context_problem, detect_profile
from nexo.setup.envfile import read_env
from nexo.setup.lock import lock
from nexo.setup.render import RENDER_DIR, render
from nexo.setup.schema import SetupError, load_casting
from tests.test_setup import MAC, ROOT, _setup

SCRIPT = ROOT / "deploy/local/ollama-setup.sh"


def _render_dir(workdir: Path, path: Path, addons=frozenset()) -> Path:
    casting = load_casting(path)
    return render(casting, lock(casting, path, workdir), workdir, MAC, addons)


def _spec(**fields):
    """An edit for _setup: set these fields under spec."""
    return lambda data: data["spec"].update(fields)


def _native(workdir, edit=None, addons=()):
    """Render the mac-native preset (edited); (compose or None, collector or None, env)."""
    out = _render_dir(workdir, _setup(workdir, "mac-native", edit), frozenset(addons))

    def read(name):
        path = out / name
        return yaml.safe_load(path.read_text()) if path.exists() else None

    return read("compose.yaml"), read("otel-collector.yaml"), read_env(out / "nexo.env")


def test_native_renders_telemetry_containers_and_no_core(workdir):
    """Nexo and Neo4j run on the host: only the collector and SigNoz are containers."""
    compose, collector, env = _native(workdir)
    services = set(compose["services"])
    assert {
        "otel-collector",
        "signoz",
        "signoz-ingester",
        "signoz-clickhouse",
    } <= services
    assert all(n == "otel-collector" or n.startswith("signoz") for n in services)
    assert not {"neo4j", "nexo"} & set(compose["services"])
    assert "nexo-data" not in compose.get("volumes", {})
    assert set(collector["service"]["pipelines"]) == {"traces", "metrics", "logs"}
    # the content-stripping collector stays in front
    assert "attributes/strip-content" in yaml.safe_dump(collector["service"])
    assert env["NEXO_SETUP_MODE"] == "native"
    assert env["NEO4J_URI"] == "bolt://localhost:7687"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:4318"
    assert env["OTEL_LOGS_EXPORTER"] == "otlp"
    assert env["LANGFUSE_HOST"] == ""
    assert env["NEXO_SECRETS_PROVIDER"] == "file" and env["NEXO_STACK"] == "nexo"
    assert "NEO4J_PASSWORD" not in env  # it stays in the store


def test_native_telemetry_off_renders_no_containers_and_a_blank_endpoint(workdir):
    """telemetry.enabled false: nexo.env alone, and no endpoint (it overrides .env's)."""
    _render_dir(workdir, _setup(workdir, "mac-single"))
    assert (workdir / RENDER_DIR / "compose.yaml").exists()
    off = _spec(telemetry={"enabled": False})
    compose, collector, env = _native(workdir, off)
    assert compose is None and collector is None  # the earlier render's files are gone
    assert sorted(p.name for p in (workdir / RENDER_DIR).iterdir()) == ["nexo.env"]
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == ""
    assert env["OTEL_LOGS_EXPORTER"] == "none"


def test_telemetry_cannot_be_off_in_compose_mode(workdir):
    """Compose runs the collector and SigNoz as core components."""
    edit = _spec(telemetry={"enabled": False})
    with pytest.raises(SetupError, match="needs target.mode native"):
        load_casting(_setup(workdir, "mac-single", edit))


@pytest.mark.parametrize(
    "addon, services, pipeline",
    [
        ("langfuse", {"langfuse-web", "langfuse-worker"}, "traces/langfuse"),
        ("jaeger", {"jaeger"}, "traces/jaeger"),
        ("hostMetrics", set(), "metrics/infra"),
        ("agentInbox", {"agent-inbox"}, None),
    ],
)
def test_native_runs_each_addon_as_a_container(workdir, addon, services, pipeline):
    """An add-on in nexo.yaml or per run joins the compose file; never neo4j or nexo."""

    def enable(data):
        data["spec"]["addons"] = {addon: {"enabled": True}}

    for compose, collector, _ in (
        _native(workdir, enable),
        _native(workdir, None, {addon}),
    ):
        assert services <= set(compose["services"])
        assert not {"neo4j", "nexo"} & set(compose["services"])
        assert (pipeline in collector["service"]["pipelines"]) == bool(pipeline)


def test_native_langfuse_points_nexo_eval_at_localhost(workdir):
    """Nexo runs on the host, so LANGFUSE_HOST names the published port, not the service."""
    edit = _spec(addons={"langfuse": {"enabled": True}}, ports={"langfuse": 13000})
    compose, _, env = _native(workdir, edit)
    assert env["LANGFUSE_HOST"] == "http://localhost:13000"
    assert "nexo" not in compose["services"]
    assert _native(workdir)[2]["LANGFUSE_HOST"] == ""


def test_native_addons_need_telemetry_for_the_collector_only(workdir):
    """Jaeger sends through the collector; Agent Inbox does not need it."""
    off = _spec(telemetry={"enabled": False}, addons={"jaeger": {"enabled": True}})
    with pytest.raises(SetupError, match="jaeger send through the collector"):
        _native(workdir, off)
    inbox = _spec(
        telemetry={"enabled": False}, addons={"agentInbox": {"enabled": True}}
    )
    compose, collector, env = _native(workdir, inbox)
    assert set(compose["services"]) == {"agent-inbox"} and collector is None
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == ""


def test_native_settings_read_the_neo4j_password_from_the_store(workdir, monkeypatch):
    """One place for the password: the store the rendered env names, not .env."""
    monkeypatch.chdir(workdir)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    store = store_from_options({"NEXO_STACK": "nexo"})
    store.put({"NEO4J_PASSWORD": "from-the-store"})
    (workdir / ".env").write_text(
        "NEO4J_PASSWORD=\nOTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318\n"
    )
    _render_dir(workdir, _setup(workdir, "mac-native"))
    settings = Settings()
    assert settings.neo4j_password.get_secret_value() == "from-the-store"
    assert settings.otel_exporter_otlp_endpoint == "http://localhost:4318"


@pytest.mark.parametrize(
    "edit, message",
    [
        (
            lambda d: d["spec"]["models"].update(provider="vllm"),
            "native.*vllm needs NVIDIA containers",
        ),
        (
            lambda d: d["spec"].update(
                patches=[
                    {
                        "target": "compose.yaml",
                        "operations": [
                            {
                                "op": "add",
                                "path": "/services/neo4j/mem_limit",
                                "value": "1g",
                            }
                        ],
                    }
                ]
            ),
            "patches.0 changes neo4j, which runs on this machine",
        ),
        (
            lambda d: d["spec"].update(
                patches=[
                    {
                        "target": "compose.yaml",
                        "operations": [
                            {
                                "op": "move",
                                "from": "/services/nexo",
                                "path": "/services/x",
                            }
                        ],
                    }
                ]
            ),
            "patches.0 changes nexo",
        ),
    ],
)
def test_native_rejects_what_cannot_work(workdir, edit, message):
    """vLLM needs NVIDIA containers; neo4j and nexo are not containers to patch."""
    with pytest.raises(SetupError, match=message):
        load_casting(_setup(workdir, "mac-native", edit))


def test_native_patch_on_a_container_service_applies(workdir):
    """Patches reach the telemetry and add-on containers."""
    limit = {"op": "add", "path": "/services/signoz/mem_limit", "value": "2g"}
    edit = _spec(patches=[{"target": "compose.yaml", "operations": [limit]}])
    assert _native(workdir, edit)[0]["services"]["signoz"]["mem_limit"] == "2g"


def test_native_patch_cannot_smuggle_in_a_core_container(workdir):
    """Replacing /services with one that has neo4j is caught after the patches apply."""
    add = {"op": "replace", "path": "/services", "value": {"neo4j": {"image": "x"}}}
    edit = _spec(patches=[{"target": "compose.yaml", "operations": [add]}])
    with pytest.raises(SetupError, match="run on this machine"):
        _native(workdir, edit)


def test_native_preflight_needs_this_machines_neo4j(workdir, monkeypatch):
    """No containers, no engine check; Neo4j must answer, with a hint to start or initialise it."""
    monkeypatch.chdir(workdir)
    path = _setup(workdir, "mac-native")
    casting = load_casting(path)
    locked = lock(casting, path, workdir)
    store = store_from_options({"NEXO_STACK": "nexo"})
    store.put({"NEO4J_PASSWORD": "pw"})
    monkeypatch.setattr(check, "context_findings", lambda _c: [])
    monkeypatch.setattr(check, "models_finding", lambda *_a: None)
    for result, words in (
        ("unreachable", "brew services start neo4j"),
        ("refused", "nexo setup neo4j-init"),
    ):
        monkeypatch.setattr(check, "neo4j_login", lambda *_a, r=result: r)
        found = {
            f.name: f for f in check.preflight(casting, locked, {"neo4j"}, MAC, store)
        }
        assert "engine" not in found and "podman vm" not in found
        assert not found["neo4j"].ok and words in found["neo4j"].detail


@pytest.fixture(name="native_up")
def _native_up(workdir, monkeypatch):
    """`nexo setup` on the mac-native preset with the engine and preflight faked."""
    seen: list[list[str]] = []

    def run(command, **_kwargs):
        seen.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.chdir(workdir)
    monkeypatch.setattr(commands.subprocess, "run", run)
    monkeypatch.setattr(commands, "detect_host", lambda _casting: MAC)
    monkeypatch.setattr(commands, "preflight", lambda *_args: [])
    _setup(workdir, "mac-native")
    return seen


def test_native_up_starts_models_then_the_telemetry_containers(native_up):
    """up runs the model script, then compose for the container services; down stops them."""
    assert cli.main(["setup", "up"]) == 0
    assert native_up[0][0].endswith("ollama-setup.sh")
    up = next(c for c in native_up if "up" in c)
    assert up[:2] == ["podman", "compose"] and "--connection" not in up
    native_up.clear()
    assert cli.main(["setup", "down"]) == 0
    assert native_up[0][:2] == ["podman", "compose"] and "down" in native_up[0]


def test_native_up_without_containers_runs_only_the_models(native_up, workdir):
    """Telemetry off and no add-ons: nothing for compose to run, and down has nothing to stop."""
    path = workdir / "nexo.yaml"
    data = yaml.safe_load(path.read_text())
    data["spec"]["telemetry"] = {"enabled": False}
    path.write_text(yaml.safe_dump(data))
    assert cli.main(["setup", "up"]) == 0
    assert [c[0].endswith("ollama-setup.sh") for c in native_up] == [True]
    assert cli.main(["setup", "down"]) == 0
    assert len(native_up) == 1


def test_native_check_warns_without_an_engine(workdir, monkeypatch):
    """Telemetry on and no compose provider: a WARN that never stops check or up."""
    monkeypatch.chdir(workdir)
    path = _setup(workdir, "mac-native")
    casting = load_casting(path)
    locked = lock(casting, path, workdir)
    store = store_from_options({"NEXO_STACK": "nexo"})
    monkeypatch.setattr(check, "context_findings", lambda _c: [])
    monkeypatch.setattr(check, "models_finding", lambda *_a: None)
    monkeypatch.setattr(check, "_run", lambda *_a: "")
    on = {n for n, enabled in locked["enabled"].items() if enabled}
    found = {f.name: f for f in check.preflight(casting, locked, on, MAC, store)}
    assert not found["engine"].ok and found["engine"].advisory
    assert "need it" in found["engine"].detail


def test_neo4j_init_sets_the_stored_password(native_up, monkeypatch, caplog):
    """neo4j-init hands neo4j-admin the store's password and never logs it."""
    monkeypatch.setattr(commands.shutil, "which", lambda _name: "/opt/neo4j-admin")
    assert cli.main(["setup", "neo4j-init"]) == 0
    password = store_from_options({"NEXO_STACK": "nexo"}).get(["NEO4J_PASSWORD"])[
        "NEO4J_PASSWORD"
    ]
    assert native_up == [["/opt/neo4j-admin", "dbms", "set-initial-password", password]]
    assert password not in caplog.text


def test_neo4j_init_is_only_for_native(workdir, monkeypatch):
    """In compose mode the container gets its password when it is created."""
    monkeypatch.chdir(workdir)
    _setup(workdir, "mac-single")
    assert cli.main(["setup", "neo4j-init"]) == 2


def test_render_drift_names_a_setup_file_changed_since_the_render_dir(
    workdir, monkeypatch
):
    """Fresh render: quiet. Edited nexo.yaml or an old render: a warning with the fix."""
    monkeypatch.chdir(workdir)
    path = _setup(workdir, "mac-single")
    _render_dir(workdir, path)
    rendered = workdir / RENDER_DIR / "nexo.env"
    assert render_drift(rendered) is None
    path.write_text(path.read_text().replace("m5-pro-24", "m1-max-64"))
    assert "nexo.yaml changed" in render_drift(rendered)
    rendered.write_text("LOCAL_PROFILE=m5-pro-24\n")
    assert "predates source tracking" in render_drift(rendered)
    assert render_drift(workdir / "missing.env") is None


def test_explain_names_each_source_and_redacts_secrets(workdir, monkeypatch):
    """environment > secrets store > rendered env > .env, then derived and default."""
    monkeypatch.chdir(workdir)
    for key in ("LOG_LEVEL", "AGENT_MAX_TURNS", "NEO4J_PASSWORD", "LOCAL_PROFILE"):
        monkeypatch.delenv(key, raising=False)
    store_from_options({"NEXO_STACK": "nexo"}).put({"NEO4J_PASSWORD": "s3cret"})
    _render_dir(workdir, _setup(workdir, "mac-native"))
    (workdir / ".env").write_text("AGENT_MAX_TURNS=7\nNEO4J_PASSWORD=old\n")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    rows = {row.name: row for row in explain(Settings())}
    assert rows["LOG_LEVEL"].source == "environment"
    assert rows["NEO4J_PASSWORD"].source == "secrets store"
    assert rows["NEO4J_PASSWORD"].value == "<redacted>"
    assert rows["LOCAL_PROFILE"].source == config.RENDERED_ENV
    assert rows["AGENT_MAX_TURNS"].source == ".env"
    assert rows["AGENT_MODEL"].source == "derived"  # from the profile
    assert "CHUNK_SIZE" not in rows  # defaults only with everything=True
    assert {r.name: r for r in explain(Settings(), everything=True)}[
        "CHUNK_SIZE"
    ].source == "default"
    assert not any("s3cret" in r.value or "old" == r.value for r in rows.values())


def test_config_command_never_prints_a_secret(workdir, monkeypatch, capsys):
    """nexo config --explain on stdout or stderr: no secret value."""
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("NEO4J_PASSWORD", "env-secret-value")
    config.get_settings.cache_clear()
    try:
        assert cli.main(["config", "--explain"]) == 0
    finally:
        config.get_settings.cache_clear()
    out = capsys.readouterr()
    assert "NEO4J_PASSWORD" in out.out + out.err
    assert "env-secret-value" not in out.out + out.err


def _fake_run(outputs: dict[str, str]):
    return lambda *command: outputs.get(command[0], "")


@pytest.mark.parametrize(
    "system, outputs, memory, expected",
    [
        ("Darwin", {"sysctl": "Apple M1 Max"}, 64.0, "m1-max-64"),
        ("Darwin", {"sysctl": "Apple M5 Pro"}, 23.6, "m5-pro-24"),
        ("Darwin", {"sysctl": "Apple M2"}, 16.0, "m2-16"),
        ("Darwin", {"sysctl": "Intel(R) Core(TM) i9"}, 32.0, None),
        ("Darwin", {"sysctl": "Apple M1"}, 8.0, None),  # below the smallest tier
        ("Linux", {"nvidia-smi": "24564\n24564"}, 128.0, "gpu-24"),
        ("Linux", {}, 62.7, "cpu-63"),
        ("Linux", {"nvidia-smi": "N/A"}, 64.0, None),
    ],
)
def test_detect_profile(system, outputs, memory, expected):
    """The chip and memory name a catalog profile; anything unknown gives None."""
    found = detect_profile(system, _fake_run(outputs), lambda: memory)
    assert (found and found[0]) == expected


@pytest.mark.parametrize(
    "argv, detected, profile",
    [
        ([], ("m1-max-64", "Apple M1 Max, 64 GB"), "m1-max-64"),
        (["--profile", "m1-pro-16"], ("m1-max-64", "x"), "m1-pro-16"),
        (["--preset", "mac-two-machines"], ("m2-max-96", "x"), "m2-max-96"),
        ([], None, "m5-pro-24"),  # unknown: the preset's own, with a warning
    ],
)
def test_init_writes_this_machines_profile(
    workdir, monkeypatch, argv, detected, profile
):
    """init fills models.local.profile from the machine unless --profile says otherwise."""
    monkeypatch.chdir(workdir)
    (workdir / "examples").symlink_to(ROOT / "examples")
    monkeypatch.setattr(commands, "detect_profile", lambda: detected)
    monkeypatch.setattr(commands.platform, "system", lambda: "Darwin")
    assert cli.main(["setup", "init", *argv]) == 0
    casting = load_casting(workdir / "nexo.yaml")
    assert casting.spec.models.local.profile == profile
    if "mac-two-machines" in argv:
        assert casting.spec.models.remote.profile == "m1-max-64"  # untouched
    assert "#" in (workdir / "nexo.yaml").read_text()  # the preset's comments stay


def test_init_rejects_an_unknown_profile(workdir, monkeypatch):
    """A typo in --profile fails before anything is written."""
    monkeypatch.chdir(workdir)
    (workdir / "examples").symlink_to(ROOT / "examples")
    assert cli.main(["setup", "init", "--profile", "m1-max-65"]) == 2
    assert not (workdir / "nexo.yaml").exists()


def test_context_problem_names_models_below_the_profile():
    """A loaded model with Ollama's 4096 default is named with the fix; enough is quiet."""
    short = context_problem(
        "http://localhost:11434/v1", "m1-max-64", "single", lambda _u: {"a": 4096}
    )
    assert "a with 4096" in short and "32768" in short and "--apply" in short
    enough = context_problem(
        "http://localhost:11434/v1", "m1-max-64", "single", lambda _u: {"a": 32768}
    )
    assert enough is None


def test_context_findings_warn_without_blocking(fake_findings):
    """An advisory finding is a WARN line and up still goes ahead."""
    up, seen, findings = fake_findings
    findings.append(Finding("ollama context", False, "short", advisory=True))
    assert up() == 0
    assert any(c[1:2] == ["compose"] and "up" in c for c in seen)


@pytest.fixture(name="fake_findings")
def _fake_findings(workdir, monkeypatch):
    seen: list[list[str]] = []
    findings: list[Finding] = []

    def run(command, **_kwargs):
        seen.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.chdir(workdir)
    monkeypatch.setattr(commands.subprocess, "run", run)
    monkeypatch.setattr(commands.time, "sleep", lambda _s: None)
    monkeypatch.setattr(commands, "neo4j_login", lambda *_a: None)
    monkeypatch.setattr(commands, "detect_host", lambda _casting: MAC)
    monkeypatch.setattr(commands, "preflight", lambda *_args: findings)
    _setup(workdir, "mac-single")
    return (lambda: cli.main(["setup", "up", "--skip-models"])), seen, findings


def test_check_reports_models_up_will_pull_as_todo(workdir, monkeypatch, caplog):
    """Before the first up, a model that is not pulled yet is a todo line, not a FAIL."""
    caplog.set_level("INFO")
    monkeypatch.chdir(workdir)

    def servers(_settings, rows):
        for row in rows:
            if row.get("model"):
                row["check"] = "model not served here"
                row["served"] = []
        return rows

    monkeypatch.setattr(commands, "check_model_servers", servers)
    _render_dir(workdir, _setup(workdir, "mac-single"))
    pending = {"qwen3.5:9b-q4_K_M", "bge-m3"}
    assert commands._models_reachable(pending)  # pylint: disable=protected-access
    assert "todo model agent: qwen3.5:9b-q4_K_M not pulled yet" in caplog.text
    assert "FAIL" not in caplog.text
    assert not commands._models_reachable(set())  # pylint: disable=protected-access


def test_podman_vm_finding_says_how_much_memory_to_set(workdir, monkeypatch):
    """Add-ons beyond the VM: the finding gives a podman machine set --memory value."""
    monkeypatch.setattr(check, "machine_memory_gb", lambda: 64.0)
    monkeypatch.setattr(check, "engine_memory_gb", lambda _host: 5.6)
    path = _setup(workdir, "mac-single")
    casting = load_casting(path)
    locked = lock(casting, path, workdir)
    on = {n for n, e in locked["enabled"].items() if e} | {"langfuse", "jaeger"}
    vm = {f.name: f for f in check.memory_findings(casting, locked, on, MAC)}[
        "podman vm"
    ]
    assert not vm.ok and "podman machine set --memory 6144" in vm.detail


@pytest.mark.skipif(not shutil.which("curl"), reason="curl not installed")
def test_setup_script_apply_sets_the_app_environment_and_a_login_agent(tmp_path):
    """--apply on macOS: launchctl setenv per setting and a LaunchAgent for reboots."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "launchctl.log"
    for name, body in (
        ("launchctl", f'echo "$@" >> "{calls}"'),
        ("uname", "echo Darwin"),
        ("ollama", "exit 0"),
        ("curl", 'case "$*" in *api/version*) exit 0 ;; *) echo "{}" ;; esac'),
    ):
        (fake_bin / name).write_text(f"#!/bin/sh\n{body}\n")
        (fake_bin / name).chmod(0o755)
    path = os.pathsep.join(
        [str(fake_bin), str(Path(sys.executable).parent), "/usr/bin", "/bin"]
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), "--apply", "m1-max-64", "heavy"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=60,
        env={"PATH": path, "HOME": str(tmp_path)},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "setenv OLLAMA_CONTEXT_LENGTH 32768" in calls.read_text()
    agent = tmp_path / "Library/LaunchAgents/dev.nexo.ollama-env.plist"
    assert "launchctl setenv OLLAMA_KV_CACHE_TYPE q8_0" in agent.read_text()
    assert "quit Ollama" in result.stdout and "NOT applied" not in result.stderr
