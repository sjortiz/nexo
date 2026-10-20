"""spec.ports: the host ports a stack publishes, and the check that names who holds one."""

from __future__ import annotations

import json
import socket

import pytest
import yaml

from nexo.secret_store import store_from_options
from nexo.setup import check
from nexo.setup.components import COMPONENTS, load_fragment
from nexo.setup.lock import lock
from nexo.setup.render import PUBLISHED, publish_ports, render
from nexo.setup.schema import Ports, SetupError, load_casting
from tests.test_setup import LINUX, MAC, ROOT, _render, _setup

MOVED = {
    "neo4jBolt": 17687,
    "neo4jHttp": 17474,
    "otlp": 14318,
    "metrics": 18889,
    "signoz": 18080,
    "langfuse": 13000,
    "langfuseStorage": 19090,
    "jaeger": 26686,
    "vllm": 18000,
    "vllmEmbedding": 18001,
}


def test_ports_move_every_published_port_and_the_urls_naming_them(workdir):
    """spec.ports: a second stack on other host ports; container ports and inner URLs stay."""
    path = _setup(workdir, "linux-vllm", lambda d: d["spec"].update(ports=MOVED))
    compose, _, env = _render(workdir, path, addons={"langfuse", "jaeger"})
    published = {
        (name, mapping)
        for name, service in compose["services"].items()
        for mapping in service.get("ports", [])
    }
    assert published == {
        ("neo4j", "127.0.0.1:17474:7474"),
        ("neo4j", "127.0.0.1:17687:7687"),
        ("otel-collector", "127.0.0.1:14318:4318"),
        ("otel-collector", "127.0.0.1:18889:8889"),
        ("signoz", "127.0.0.1:18080:8080"),
        ("langfuse-web", "127.0.0.1:13000:3000"),
        ("langfuse-storage", "127.0.0.1:19090:8333"),
        ("jaeger", "127.0.0.1:26686:16686"),
        ("vllm-inference-service", "127.0.0.1:18000:8000"),
        ("vllm-embedding-service", "127.0.0.1:18001:8000"),
    }
    web = compose["services"]["langfuse-web"]["environment"]
    assert web["NEXTAUTH_URL"] == "http://localhost:13000"
    assert web["LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT"] == "http://localhost:19090"
    worker = compose["services"]["langfuse-worker"]["environment"]
    assert worker["LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT"] == "http://langfuse-storage:8333"
    assert (
        compose["services"]["nexo"]["environment"]["NEO4J_URI"] == "neo4j://neo4j:7687"
    )
    assert env["NEO4J_URI"] == "bolt://localhost:17687"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:14318"
    assert env["VLLM_BASE_URL"] == "http://localhost:18000/v1"
    assert env["EMBEDDING_BASE_URL"] == "http://localhost:18001/v1"
    models = yaml.safe_load((workdir / "nexo.lock").read_text())["models"]
    assert {m["server"] for m in models.values()} == {
        "http://localhost:18000/v1",
        "http://localhost:18001/v1",
    }


def test_every_port_a_fragment_publishes_has_a_ports_field():
    """A published port nobody can move would collide with a second stack."""
    for component in COMPONENTS.values():
        if component.compose is None:
            continue
        services = load_fragment(ROOT, component.compose).get("services", {})
        for name, service in services.items():
            for mapping in service.get("ports", []):
                assert (name, int(mapping.rsplit(":", 1)[1])) in PUBLISHED, mapping
    services = {"neo4j": {"ports": ["7687:7687"]}}
    publish_ports(services, Ports.model_validate({"neo4jBolt": 17687}))
    assert services["neo4j"]["ports"] == ["17687:7687"]
    with pytest.raises(SetupError, match="redis publishes 6379"):
        publish_ports({"redis": {"ports": ["127.0.0.1:6379:6379"]}}, Ports())


def test_ports_finding_names_who_holds_a_port(monkeypatch):
    """A port another stack or process holds fails check; this stack's own pass."""
    engine = {"ps": ""}
    monkeypatch.setattr(
        check,
        "_run",
        lambda *command: "p42\ncpython3" if command[0] == "lsof" else engine["ps"],
    )
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        free = spare.getsockname()[1]
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        port = held.getsockname()[1]
        mappings = [f"127.0.0.1:{port}:7687", f"127.0.0.1:{free}:7474"]
        compose = {"name": "nexo", "services": {"neo4j": {"ports": mappings}}}

        def podman(project: str) -> str:
            labels = {"com.docker.compose.project": project}
            ports = [{"host_port": port - 1, "range": 2}]
            containers = [{"Names": ["n-1"], "Labels": labels, "Ports": ports}]
            return json.dumps(containers, indent=4)  # as podman ps prints it

        engine["ps"] = podman("nexo")
        assert check.ports_finding(compose, MAC).ok
        engine["ps"] = podman("other")
        finding = check.ports_finding(compose, MAC)
        assert not finding.ok and f"{port} (n-1, stack other)" in finding.detail
        assert str(free) not in finding.detail
        engine["ps"] = json.dumps(
            {
                "Names": "d-1",
                "Labels": "com.docker.compose.project=nexo,a=b",
                "Ports": f"127.0.0.1:{port}->7687/tcp",
            }
        )
        assert check.ports_finding(compose, LINUX).ok
        engine["ps"] = ""
        finding = check.ports_finding(compose, MAC)
        assert not finding.ok and f"{port} (python3)" in finding.detail
    assert check.ports_finding(compose, MAC).ok  # released
    assert "ports" in check.FORCEABLE


def test_preflight_reports_a_held_port_before_compose_fails_on_it(workdir, monkeypatch):
    """check and up name the port another process holds."""
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(check, "_run", lambda *_command: "")
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        port = held.getsockname()[1]
        path = _setup(
            workdir, "mac-single", lambda d: d["spec"].update(ports={"signoz": port})
        )
        casting = load_casting(path)
        locked = lock(casting, path, workdir)
        render(casting, locked, workdir, MAC)
        store = store_from_options({"NEXO_STACK": "nexo"})
        findings = check.preflight(casting, locked, {"signoz"}, MAC, store)
    ports = [f for f in findings if f.name == "ports"]
    assert len(ports) == 1 and not ports[0].ok and str(port) in ports[0].detail
