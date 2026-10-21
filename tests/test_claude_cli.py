"""JSON roles served by the Claude Code CLI (`claude-cli://`)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from nexo.agents.claude_cli_agent import (
    ClaudeCliInvestigator,
    normalize_citations,
    read_events,
)
from nexo.agents.models import InvestigationScope
from nexo.claude_cli import ClaudeCliChatModel, ClaudeCliError, reply_text, transcript
from nexo.llm import build_chat_model, invoke_structured
from nexo.storage.schemas import Sensitivity
from nexo.storage.vector_store import ChunkFilter


class Ok(BaseModel):
    """A tiny response schema."""

    ok: bool


def _done(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(["claude"], returncode, stdout, stderr)


def test_command_is_isolated_and_structured():
    """One tool-less, setting-less, unsaved turn with the schema as --json-schema."""
    cmd = ClaudeCliChatModel(model="claude-sonnet-5-5").command(
        "sys",
        {
            "type": "json_schema",
            "json_schema": {"name": "Ok", "schema": {"type": "object"}},
        },
    )
    assert cmd[1:5] == ["-p", "--model", "claude-sonnet-5-5", "--output-format"]
    for flag in ("--no-session-persistence", "--strict-mcp-config"):
        assert flag in cmd
    assert (
        cmd[cmd.index("--tools") + 1] == ""
        and cmd[cmd.index("--setting-sources") + 1] == ""
    )
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == {"type": "object"}


def test_transcript_keeps_retry_feedback():
    """A retry carries the earlier reply and the validation error."""
    assert transcript([SystemMessage("s"), HumanMessage("q")]) == "q"
    retry = transcript([HumanMessage("q"), AIMessage("bad"), HumanMessage("fix it")])
    assert retry == "User: q\n\nAssistant: bad\n\nUser: fix it"


def test_reply_prefers_structured_output_and_surfaces_errors():
    """Structured output wins over the text result; CLI errors raise."""
    out = json.dumps(
        {"result": "x", "structured_output": {"ok": True}, "is_error": False}
    )
    assert json.loads(reply_text(_done(out))) == {"ok": True}
    assert reply_text(_done(json.dumps({"result": "plain"}))) == "plain"
    with pytest.raises(ClaudeCliError, match="reported an error"):
        reply_text(_done(json.dumps({"is_error": True, "result": "rate limited"})))
    with pytest.raises(ClaudeCliError, match="exited 1"):
        reply_text(_done("", returncode=1, stderr="not logged in"))


def test_build_chat_model_and_invoke_structured(monkeypatch, settings):
    """`claude-cli://` builds the CLI model; invoke_structured gets validated output."""
    settings.extraction_base_url = "claude-cli://"
    settings.extraction_model = "claude-sonnet-5-5"
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(cmd=cmd, prompt=kwargs["input"])
        return _done(json.dumps({"structured_output": {"ok": True}}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    llm = build_chat_model(settings, role="extraction")
    assert isinstance(llm, ClaudeCliChatModel)
    result = invoke_structured(
        llm, Ok, [SystemMessage("be brief"), HumanMessage("ok?")]
    )
    assert result.ok is True and seen["prompt"] == "ok?"
    assert seen["cmd"][seen["cmd"].index("--system-prompt") + 1] == "be brief"


# --- the investigator over the nexo MCP tools (AGENT_BASE_URL=claude-cli://) ---------

C1, C2, C3 = ("chk_" + c * 16 for c in "123")


def _events(*events):
    return [json.dumps(e) for e in events]


def test_read_events_builds_the_audit_trail():
    """Only nexo_get_chunks results are evidence; withheld ids and tool calls are kept."""
    lines = _events(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "mcp__nexo__nexo_search",
                        "input": {},
                    },
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "mcp__nexo__nexo_get_chunks",
                        "input": {},
                    },
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": json.dumps(
                            {"result": [{"chunk_id": C3, "snippet": "s"}]}
                        ),
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "t2",
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {
                                        "chunks": [{"chunk_id": C1, "text": "t"}],
                                        "withheld": [C2],
                                    }
                                ),
                            }
                        ],
                    },
                ]
            },
        },
        {
            "type": "result",
            "result": f"Yes [chunk:{C1}] and [chunk:{C3}].",
            "is_error": False,
            "usage": {"output_tokens": 9},
        },
    )
    out = read_events(lines + ["not json"])
    assert [t.tool for t in out.tool_calls] == ["nexo_search", "nexo_get_chunks"]
    assert out.fetched_chunk_ids == [C1] and out.withheld_chunk_ids == [C2]
    assert out.citations == [C1, C3]  # C3 was only a snippet: compliance flags it
    assert out.stop_reason == "end_turn" and out.usage == {"output_tokens": 9}


def test_investigator_runs_mcp_at_the_callers_clearance(monkeypatch, chunk_store):
    """The MCP server gets the caller's clearance; fetched sensitivity comes from the store."""
    seen = {}

    def fake_run(cmd, **kwargs):
        config = json.loads(
            Path(cmd[cmd.index("--mcp-config") + 1]).read_text(encoding="utf-8")
        )
        seen.update(
            cmd=cmd, env=config["mcpServers"]["nexo"]["env"], prompt=kwargs["input"]
        )
        return _done(
            "\n".join(
                _events({"type": "result", "result": "no data", "is_error": False})
            )
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    agent = ClaudeCliInvestigator(
        model="claude-sonnet-5-5", chunks=chunk_store, timeout_s=5
    )
    scope = InvestigationScope(
        chunk_filter=ChunkFilter(max_sensitivity=Sensitivity.PUBLIC, doc_types=["memo"])
    )
    out = agent.investigate("q?", scope=scope)
    assert seen["env"] == {"MCP_CLEARANCE": "public", "MCP_REVIEW_TOOLS": "false"}
    assert seen["cmd"][seen["cmd"].index("--allowedTools") + 1] == "mcp__nexo__*"
    assert "memo" in seen["prompt"] and out.answer == "no data"


@pytest.mark.parametrize(
    "loose, canonical",
    [
        (f"A [chunk_id:{C1}].", f"A [chunk:{C1}]."),
        ("B [chunk:" + "1" * 16 + "].", f"B [chunk:{C1}]."),  # dropped chk_ prefix
        ("C [chunk:<chunk_id>] x", "C  x"),  # placeholder, no id
        (f"D [chunk: {C1}, {C2}]", f"D [chunk:{C1}][chunk:{C2}]"),
        (f"E (chunk:{C3})", f"E [chunk:{C3}]"),
        ("F [1] and [Notes]", "F [1] and [Notes]"),  # not citations
    ],
)
def test_loose_citations_are_normalized(loose, canonical):
    """Variant citation forms become [chunk:chk_...] so compliance can check them."""
    assert normalize_citations(loose) == canonical
