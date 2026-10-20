"""`nexo models`: what each role resolves to, and whether its server actually serves that model."""

from __future__ import annotations

import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic import SecretStr

from nexo import cli
from nexo import local_profiles as lp
from nexo.config import Settings, get_settings
from nexo.llm import check_model_servers, resolved_models


class _ModelsAPI(BaseHTTPRequestHandler):
    """OpenAI-style GET /v1/models and POST /v1/chat/completions.

    `served` maps a URL prefix to the model ids listed there; `replies` maps a model to how its
    chat completion answers: "json" (default), "http-501", "prose" or "truncated".
    """

    served: dict[str, list[str]] = {}
    replies: dict[str, str] = {}
    requests: list[tuple[str, str]] = []
    posts: list[dict] = []

    def do_POST(self):  # noqa: N802 - http.server API
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).posts.append(body)
        mode = self.replies.get(body["model"], "json")
        if mode == "http-501":
            self.send_error(501, "structured output is unavailable")
            return
        content = {"json": '{"ok": true}', "prose": 'Sure! {"ok": true}', "truncated": '{"ok": tr'}[mode]
        reply = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": content}}]})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(reply.encode())

    def do_GET(self):  # noqa: N802 - http.server API
        type(self).requests.append((self.path, self.headers.get("Authorization", "")))
        prefix = self.path.removesuffix("/models")
        if prefix not in self.served or not self.path.endswith("/models"):
            self.send_error(404)
            return
        body = json.dumps({"object": "list", "data": [{"id": m, "object": "model"} for m in self.served[prefix]]})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def models_api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ModelsAPI)
    _ModelsAPI.served, _ModelsAPI.replies, _ModelsAPI.requests, _ModelsAPI.posts = {}, {}, [], []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _closed_port_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


def _by_role(rows):
    return {row["role"]: row for row in rows}


def test_two_machines_place_heavy_roles_on_the_remote_and_the_rest_locally():
    settings = Settings(_env_file=None, local_profile="m5-pro-24", remote_profile="m1-max-64",
                        local_base_url="http://local:11434/v1", remote_base_url="http://tunnel:11435/v1")
    rows = _by_role(resolved_models(settings))
    heavy, light = lp.plan("m1-max-64", "heavy").roles, lp.plan("m5-pro-24", "light").roles
    assert (rows["agent"]["model"], rows["agent"]["base_url"]) == (heavy["agent"], "http://tunnel:11435/v1")
    assert (rows["extraction"]["model"], rows["extraction"]["base_url"]) == (heavy["extraction"], "http://tunnel:11435/v1")
    for role in ("router", "judge", "embedding"):
        assert (rows[role]["model"], rows[role]["base_url"]) == (light[role], "http://local:11434/v1"), role
    assert rows["agent"]["model"] != rows["router"]["model"]
    # The 64 GB agent model is a thinking model: reasoning stays on for the agent, off for extraction.
    assert rows["agent"]["reasoning_effort"] is None
    assert rows["extraction"]["reasoning_effort"] == "none"


def test_explicit_settings_win_over_the_profile():
    settings = Settings(_env_file=None, local_profile="m1-max-64", agent_model="my-agent",
                        router_base_url="http://elsewhere:9000/v1")
    rows = _by_role(resolved_models(settings))
    assert rows["agent"]["model"] == "my-agent"
    assert rows["agent"]["base_url"] == "http://localhost:11434/v1"  # the rest still from the profile
    assert rows["router"]["base_url"] == "http://elsewhere:9000/v1"
    assert rows["router"]["model"] == lp.plan("m1-max-64").roles["router"]


def test_without_a_profile_roles_fall_back_to_the_vllm_defaults():
    rows = _by_role(resolved_models(Settings(_env_file=None, vllm_base_url="http://gpu:8000/v1", vllm_model="big")))
    assert {rows[r]["model"] for r in ("agent", "extraction", "router")} == {"big"}
    assert rows["agent"]["base_url"] == "http://gpu:8000/v1"
    assert rows["judge"]["model"] is None and "off" in rows["judge"]["note"]


def test_disabled_judge_and_fake_embeddings_are_reported_not_hidden():
    settings = Settings(_env_file=None, local_profile="m1-pro-16", embedding_provider="fake")
    rows = _by_role(resolved_models(settings))
    assert lp.plan("m1-pro-16").roles["judge"] is None
    assert rows["judge"]["model"] is None
    assert rows["embedding"]["model"] is None and rows["embedding"]["note"].startswith("fake")
    assert "base_url" not in rows["judge"]


def test_output_never_contains_api_keys():
    settings = Settings(_env_file=None, vllm_api_key=SecretStr("sk-default-secret"),
                        agent_api_key=SecretStr("sk-agent-secret"), embedding_api_key=SecretStr("sk-embed-secret"))
    text = json.dumps(resolved_models(settings))
    assert "secret" not in text


def test_check_marks_served_missing_and_unreachable(models_api):
    _ModelsAPI.served = {"/local/v1": ["qwen3:4b-instruct-2507-q4_K_M", "bge-m3:latest"], "/remote/v1": ["other-model"]}
    settings = Settings(_env_file=None, local_profile="m5-pro-24", remote_profile="m1-max-64",
                        local_base_url=f"{models_api}/local/v1", remote_base_url=f"{models_api}/remote/v1",
                        judge_base_url=_closed_port_url())
    rows = _by_role(check_model_servers(settings, resolved_models(settings)))
    assert rows["router"]["check"] == "ok"
    assert rows["embedding"]["check"] == "ok"  # pulled as bge-m3, listed as bge-m3:latest
    assert rows["agent"]["check"] == "model not served here"
    assert rows["agent"]["served"] == ["other-model"]
    assert rows["judge"]["check"].startswith("unreachable")
    # One request per server, with the role's key (profiles use "ollama").
    paths = [path for path, _ in _ModelsAPI.requests]
    assert sorted(paths) == ["/local/v1/models", "/remote/v1/models"]
    assert {auth for _, auth in _ModelsAPI.requests} == {"Bearer ollama"}


def test_check_does_not_accept_a_different_tag(models_api):
    _ModelsAPI.served = {"/v1": ["qwen3.5:9b-q8_0"]}
    settings = Settings(_env_file=None, vllm_base_url=f"{models_api}/v1", vllm_model="qwen3.5:9b-q4_K_M",
                        embedding_provider="fake")
    rows = _by_role(check_model_servers(settings, resolved_models(settings)))
    assert rows["agent"]["check"] == "model not served here"


def test_check_reports_a_server_that_is_not_an_openai_api(models_api):
    settings = Settings(_env_file=None, vllm_base_url=f"{models_api}/nothing-here/v1", embedding_provider="fake")
    rows = _by_role(check_model_servers(settings, resolved_models(settings)))
    assert rows["agent"]["check"].startswith("unreachable: HTTPError")


def _one_machine(models_api, model="qwen3.5:9b-q4_K_M"):
    _ModelsAPI.served = {"/v1": [model, "bge-m3:latest"]}
    return Settings(_env_file=None, local_profile="m5-pro-24", local_base_url=f"{models_api}/v1")


def test_check_probes_structured_output_once_per_json_model(models_api):
    settings = _one_machine(models_api)
    rows = _by_role(check_model_servers(settings, resolved_models(settings)))
    assert {r: rows[r]["check"] for r in ("agent", "extraction", "router", "embedding")} == dict.fromkeys(
        ("agent", "extraction", "router", "embedding"), "ok")
    # extraction and router share the model, server and reasoning setting: one probe.
    assert len(_ModelsAPI.posts) == 1
    probe = _ModelsAPI.posts[0]
    assert probe["model"] == "qwen3.5:9b-q4_K_M"
    assert probe["response_format"]["type"] == "json_schema"
    assert probe["response_format"]["json_schema"]["schema"]["required"] == ["ok"]
    assert probe["reasoning_effort"] == "none"  # thinking model on a JSON role


@pytest.mark.parametrize(("mode", "detail"), [("http-501", "HTTP 501"), ("prose", "not schema-valid"),
                                              ("truncated", "not schema-valid")])
def test_check_fails_json_roles_whose_server_cannot_constrain_output(models_api, mode, detail):
    settings = _one_machine(models_api)
    _ModelsAPI.replies = {"qwen3.5:9b-q4_K_M": mode}
    rows = _by_role(check_model_servers(settings, resolved_models(settings)))
    for role in ("extraction", "router"):
        assert rows[role]["check"] == "structured output failed"
        assert detail in rows[role]["detail"]
    assert rows["agent"]["check"] == "ok"  # the agent calls tools; it is not probed for JSON
    assert rows["embedding"]["check"] == "ok"


def test_check_does_not_probe_a_model_that_is_not_served(models_api):
    settings = _one_machine(models_api, model="something-else")
    rows = _by_role(check_model_servers(settings, resolved_models(settings)))
    assert rows["router"]["check"] == "model not served here"
    assert _ModelsAPI.posts == []


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env from the repository
    for key in ("LOCAL_PROFILE", "REMOTE_PROFILE", "VLLM_BASE_URL", "VLLM_MODEL", "EMBEDDING_PROVIDER",
                "AGENT_MODEL", "JUDGE_MODEL", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LOG_FORMAT", "json")
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()
    logging.getLogger().handlers.clear()  # the CLI bound a handler to this test's captured stdout


def _cli_record(capsys) -> dict:
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    return next(entry for entry in lines if entry["logger"] == "nexo.cli")


def test_cli_exit_code_follows_the_check(cli_env, models_api, capsys):
    _ModelsAPI.served = {"/v1": ["served-model"]}
    cli_env.setenv("VLLM_BASE_URL", f"{models_api}/v1")
    cli_env.setenv("EMBEDDING_PROVIDER", "fake")
    cli_env.setenv("VLLM_MODEL", "served-model")
    assert cli.main(["models", "--check"]) == 0
    record = _cli_record(capsys)
    assert record["level"] == "INFO" and "all reachable" in record["message"]

    get_settings.cache_clear()
    cli_env.setenv("AGENT_MODEL", "missing-model")
    assert cli.main(["models", "--check"]) == 1
    record = _cli_record(capsys)
    assert record["level"] == "ERROR"
    assert "agent" in record["message"] and "extraction" not in record["message"]
    assert _by_role(record["result"]["roles"])["agent"]["served"] == ["served-model"]


def test_cli_without_check_does_not_contact_servers(cli_env, capsys):
    cli_env.setenv("LOCAL_PROFILE", "m1-max-64")
    cli_env.setenv("VLLM_BASE_URL", _closed_port_url())
    assert cli.main(["models"]) == 0
    record = _cli_record(capsys)
    assert "LOCAL_PROFILE=m1-max-64" in record["message"]
    assert record["result"]["local_profile"] == "m1-max-64"
    assert all("check" not in row for row in record["result"]["roles"])
