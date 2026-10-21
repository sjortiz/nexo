"""Model clients for any OpenAI-compatible server (Ollama or vLLM, ADR-0012).

The servers speak the OpenAI API, so:
* LangChain talks to it through `ChatOpenAI(base_url=...)` (ingestion, router).
* Strands talks to it through `strands.models.openai.OpenAIModel` (agent).
* Embeddings come from a separate OpenAI-compatible pooling deployment.

Structured output uses vLLM guided decoding (`response_format` json_schema),
so tokens are constrained to the Pydantic schema; we still validate with
Pydantic and feed validation errors back for a bounded retry.
"""

from __future__ import annotations

import json
import re
import shutil
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, TypeVar

from langchain_core.embeddings import DeterministicFakeEmbedding, Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pydantic import BaseModel, ValidationError
from strands.models.openai import OpenAIModel

from nexo.claude_cli import SCHEME as CLAUDE_CLI
from nexo.claude_cli import ClaudeCliChatModel
from nexo.config import ModelRole, Settings

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class StructuredOutputError(RuntimeError):
    """The model did not produce schema-valid JSON within the retry budget."""


class ModelServerError(RuntimeError):
    """A model server did not answer, or answered with something unreadable."""


def build_chat_model(
    settings: Settings, *, role: ModelRole, temperature: float = 0.0
) -> BaseChatModel:
    """LangChain client for a JSON-producing role (router, extraction, judge).

    A base URL of `claude-cli://` serves the role through the Claude Code CLI.
    """
    endpoint = settings.endpoint(role)
    if endpoint.base_url.startswith(CLAUDE_CLI):
        return ClaudeCliChatModel(
            model=endpoint.model, timeout_s=settings.llm_timeout_s
        )
    return ChatOpenAI(
        base_url=endpoint.base_url,
        api_key=endpoint.api_key,
        model=endpoint.model,
        reasoning_effort=endpoint.reasoning_effort,
        temperature=temperature,
        timeout=settings.llm_timeout_s,
        # The openai client retries connection errors, 408/409/429 and 5xx
        # with exponential backoff - this covers vLLM pod restarts.
        max_retries=settings.llm_max_retries,
    )


def build_embeddings(settings: Settings) -> Embeddings:
    """Return the configured embeddings client (OpenAI-compatible or fake)."""
    if settings.embedding_provider == "fake":
        return DeterministicFakeEmbedding(size=settings.embedding_dimensions)
    return OpenAIEmbeddings(
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key,
        model=settings.embedding_model,
        # Non-OpenAI servers take raw strings; the default path pre-tokenises
        # with tiktoken and sends token ids, which vLLM/TEI reject.
        check_embedding_ctx_length=False,
        max_retries=settings.llm_max_retries,
        timeout=settings.llm_timeout_s,
    )


def build_strands_model(settings: Settings) -> OpenAIModel:
    """Strands model provider pointed at vLLM.

    vLLM must run with `--enable-auto-tool-choice --tool-call-parser llama3_json`
    for Llama 3.x tool calling; without it the model answers in plain text and
    the agent never calls its tools.
    """
    endpoint = settings.endpoint("agent")
    return OpenAIModel(
        client_args={
            "base_url": endpoint.base_url,
            "api_key": endpoint.api_key.get_secret_value(),
            "timeout": settings.llm_timeout_s,
            "max_retries": settings.llm_max_retries,
        },
        model_id=endpoint.model,
        params={
            "temperature": settings.agent_temperature,
            "max_tokens": settings.agent_max_tokens,
        },
    )


def invoke_structured(
    llm: BaseChatModel,
    schema: type[T],
    messages: Sequence[BaseMessage],
    *,
    max_attempts: int = 3,
    config: RunnableConfig | None = None,
) -> T:
    """Call the LLM with guided JSON decoding and return a validated model.

    Transport failures are retried inside the client; this loop handles the
    semantic failure mode (JSON that violates the schema's validators) by
    showing the model its own output and the validation error.
    """
    bound = llm.bind(
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "schema": schema.model_json_schema(),
            },
        }
    )
    history = list(messages)
    last_error: Exception | None = None
    for _ in range(max_attempts):
        reply = bound.invoke(history, config=config)
        content = (
            reply.content
            if isinstance(reply.content, str)
            else json.dumps(reply.content)
        )
        try:
            return schema.model_validate_json(_FENCE.sub("", content).strip())
        except ValidationError as exc:
            last_error = exc
            history += [
                AIMessage(content=content),
                HumanMessage(
                    content=(
                        "Your JSON failed validation:\n"
                        f"{exc.errors(include_url=False, include_input=False)}\n"
                        "Return only corrected JSON that satisfies the schema."
                    )
                ),
            ]
    raise StructuredOutputError(
        f"{schema.__name__}: no valid output after {max_attempts} attempts"
    ) from last_error


@dataclass(frozen=True)
class ModelRow:  # pylint: disable=too-many-instance-attributes  # one report row
    """One role's model and server, and what `--check` found out about them. No secrets."""

    role: str
    model: str | None
    base_url: str | None = None
    reasoning_effort: str | None = None
    note: str | None = None  # why a role is off
    check: str | None = (
        None  # ok | unreachable: ... | model not served here | structured output failed
    )
    served: list[str] | None = (
        None  # what the server lists, when the model is not among them
    )
    detail: str | None = None  # the structured-output probe's complaint

    def as_dict(self) -> dict[str, Any]:
        """The row as `nexo models` prints it: only the fields that apply to the role."""
        row: dict[str, Any] = {"role": self.role, "model": self.model}
        if self.model is not None:
            row["base_url"] = self.base_url
            if self.role != "embedding":
                row["reasoning_effort"] = self.reasoning_effort
        for key in ("note", "check", "served", "detail"):
            if (value := getattr(self, key)) is not None:
                row[key] = value
        return row


def resolved_models(settings: Settings) -> list[ModelRow]:
    """The model and server each role calls, after profiles and explicit settings. No secrets."""
    rows = []
    for role in ("agent", "extraction", "router"):
        endpoint = settings.endpoint(role)
        rows.append(
            ModelRow(role, endpoint.model, endpoint.base_url, endpoint.reasoning_effort)
        )
    if settings.judge_model:
        endpoint = settings.endpoint("judge")
        rows.append(
            ModelRow(
                "judge", endpoint.model, endpoint.base_url, endpoint.reasoning_effort
            )
        )
    else:
        rows.append(
            ModelRow("judge", None, note="off: nexo eval skips generation scores")
        )
    if settings.embedding_provider == "fake":
        rows.append(
            ModelRow("embedding", None, note="fake: deterministic offline vectors")
        )
    else:
        rows.append(
            ModelRow("embedding", settings.embedding_model, settings.embedding_base_url)
        )
    return rows


JSON_ROLES = ("extraction", "router", "judge")
_PROBE_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def check_model_servers(
    settings: Settings, rows: list[ModelRow], *, timeout_s: float = 5.0
) -> list[ModelRow]:
    """Each row with whether its server answers and lists its model.

    Uses GET /models (OpenAI protocol). JSON roles also get one tiny
    schema-constrained request: a server can list a model and still be unable
    to constrain its output (e.g. Ollama MLX builds), which breaks extraction
    and routing.
    """
    listings: dict[str, set[str]] = {}
    unreachable: dict[str, str] = {}
    probes: dict[tuple, str | None] = {}
    checked = []
    for row in rows:
        if not row.model:
            checked.append(row)
            continue
        if row.base_url.startswith(CLAUDE_CLI):
            # No server to ask; the CLI's own sign-in serves the model.
            found = shutil.which("claude") is not None
            checked.append(
                replace(row, check="ok" if found else "claude CLI not found")
            )
            continue
        key = (
            settings.embedding_api_key
            if row.role == "embedding"
            else settings.endpoint(row.role).api_key
        )
        url = row.base_url
        if url not in listings and url not in unreachable:
            try:
                listings[url] = _served_models(url, key.get_secret_value(), timeout_s)
            except ModelServerError as exc:
                unreachable[url] = str(exc)
        served = listings.get(url)
        if served is None:
            row = replace(row, check=f"unreachable: {unreachable[url]}")
        # Ollama tags untagged pulls :latest
        elif row.model in served or f"{row.model}:latest" in served:
            row = replace(row, check="ok")
        else:
            row = replace(row, check="model not served here", served=sorted(served))
        if row.check == "ok" and row.role in JSON_ROLES:
            probe = (row.base_url, row.model, row.reasoning_effort)
            if probe not in probes:
                probes[probe] = _probe_structured_output(
                    *probe, key.get_secret_value(), settings.llm_timeout_s
                )
            if probes[probe]:
                row = replace(
                    row, check="structured output failed", detail=probes[probe]
                )
        checked.append(row)
    return checked


def _probe_structured_output(
    base_url: str,
    model: str,
    reasoning_effort: str | None,
    api_key: str,
    timeout_s: float,
) -> str | None:
    """Return None if a minimal request yields schema-valid JSON, else what went wrong."""
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": 64,
        "messages": [
            {"role": "user", "content": 'Reply with the JSON object {"ok": true}.'}
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "probe", "schema": _PROBE_SCHEMA},
        },
    }
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            content = json.load(response)["choices"][0]["message"]["content"] or ""
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:200]}"
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        return f"{type(exc).__name__}: {exc}"
    try:
        parsed = json.loads(content)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("ok"), bool):
        return None
    return f"reply is not schema-valid JSON: {content[:80]!r}"


def _served_models(base_url: str, api_key: str, timeout_s: float) -> set[str]:
    """The model ids the server lists at GET /models."""
    request = urllib.request.Request(
        base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {api_key}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return {entry.get("id") for entry in json.load(response).get("data", [])}
    # URLError, HTTPError and timeouts are OSError; bad JSON is ValueError.
    except (OSError, ValueError) as exc:
        raise ModelServerError(f"{type(exc).__name__}: {exc}") from exc
