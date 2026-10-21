"""A JSON role (extraction, router, judge) served by the Claude Code CLI.

Set a role's base URL to `claude-cli://` and its model to a Claude model id:

    EXTRACTION_BASE_URL=claude-cli://
    EXTRACTION_MODEL=claude-sonnet-5-5

Each call runs `claude -p` once, signed in as the local Claude Code user (no API
key), with no tools, MCP servers, settings or saved session, and passes the
response schema to `--json-schema` so the reply is structured output. Calls run
in parallel (EXTRACTION_CONCURRENCY). The text goes to Anthropic, so use it only
where that data flow is acceptable. The agent role (Strands tool calling) still
needs an OpenAI-compatible server.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult

SCHEME = "claude-cli://"


class ClaudeCliError(RuntimeError):
    """The CLI failed, timed out or reported an error."""


class ClaudeCliChatModel(
    BaseChatModel
):  # pylint: disable=abstract-method  # no streaming or tools
    """LangChain chat model that answers through `claude -p`."""

    model: str
    timeout_s: float = 300.0
    binary: str = "claude"

    @property
    def _llm_type(self) -> str:
        return "claude-cli"

    def command(self, system: str, response_format: dict | None) -> list[str]:
        """The CLI invocation: one isolated, tool-less, structured turn."""
        cmd = [
            shutil.which(self.binary) or self.binary,
            "-p",
            "--model",
            self.model,
            "--output-format",
            "json",
            "--no-session-persistence",
            "--tools",
            "",
            "--strict-mcp-config",
            "--setting-sources",
            "",
        ]
        if system:
            cmd += ["--system-prompt", system]
        if response_format and response_format.get("type") == "json_schema":
            schema = response_format["json_schema"]["schema"]
            cmd += ["--json-schema", json.dumps(schema)]
        return cmd

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        system = "\n\n".join(
            str(m.content) for m in messages if isinstance(m, SystemMessage)
        )
        cmd = self.command(system, kwargs.get("response_format"))
        try:
            done = subprocess.run(
                cmd,
                input=transcript(messages),
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ClaudeCliError(
                f"claude -p timed out after {self.timeout_s}s"
            ) from exc
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=reply_text(done)))]
        )


def transcript(messages: list[BaseMessage]) -> str:
    """The conversation after the system prompt, as one prompt.

    A first call is just the user message; a retry after a validation error
    also carries the earlier reply and the error, labelled by speaker.
    """
    turns = [m for m in messages if not isinstance(m, SystemMessage)]
    if len(turns) == 1:
        return str(turns[0].content)
    return "\n\n".join(
        f"{'Assistant' if isinstance(m, AIMessage) else 'User'}: {m.content}"
        for m in turns
    )


def reply_text(done: subprocess.CompletedProcess) -> str:
    """The structured output (as JSON text) or the plain result of one CLI run."""
    if done.returncode != 0 and not done.stdout.strip():
        raise ClaudeCliError(
            f"claude -p exited {done.returncode}: {done.stderr[-300:]}"
        )
    try:
        result = json.loads(done.stdout)
    except ValueError as exc:
        raise ClaudeCliError(
            f"claude -p printed no JSON: {done.stdout[-300:]}"
        ) from exc
    if result.get("is_error"):
        raise ClaudeCliError(
            f"claude -p reported an error: {str(result.get('result'))[:300]}"
        )
    if result.get("structured_output") is not None:
        return json.dumps(result["structured_output"])
    return str(result.get("result") or "")
