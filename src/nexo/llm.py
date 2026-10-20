"""Model clients for the self-hosted vLLM layer.

vLLM serves an OpenAI-compatible API, so:
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
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import TYPE_CHECKING, TypeVar

from langchain_core.embeddings import DeterministicFakeEmbedding, Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pydantic import BaseModel, ValidationError

from nexo.config import ModelRole, Settings

if TYPE_CHECKING:
    from strands.models.openai import OpenAIModel

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class StructuredOutputError(RuntimeError):
    """The model did not produce schema-valid JSON within the retry budget."""


def build_chat_model(settings: Settings, *, role: ModelRole, temperature: float = 0.0) -> ChatOpenAI:
    """LangChain client for a JSON-producing role (router, extraction, judge)."""
    endpoint = settings.endpoint(role)
    extra: dict[str, str] = {}
    if endpoint.reasoning_effort:
        extra["reasoning_effort"] = endpoint.reasoning_effort
    return ChatOpenAI(
        **extra,
        base_url=endpoint.base_url,
        api_key=endpoint.api_key,
        model=endpoint.model,
        temperature=temperature,
        timeout=settings.llm_timeout_s,
        # The openai client retries connection errors, 408/409/429 and 5xx
        # with exponential backoff - this covers vLLM pod restarts.
        max_retries=settings.llm_max_retries,
    )


def build_embeddings(settings: Settings) -> Embeddings:
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
    from strands.models.openai import OpenAIModel

    endpoint = settings.endpoint("agent")
    return OpenAIModel(
        client_args={
            "base_url": endpoint.base_url,
            "api_key": endpoint.api_key.get_secret_value(),
            "timeout": settings.llm_timeout_s,
            "max_retries": settings.llm_max_retries,
        },
        model_id=endpoint.model,
        params={"temperature": settings.agent_temperature, "max_tokens": settings.agent_max_tokens},
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
            "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()},
        }
    )
    history = list(messages)
    last_error: Exception | None = None
    for _ in range(max_attempts):
        reply = bound.invoke(history, config=config)
        content = reply.content if isinstance(reply.content, str) else json.dumps(reply.content)
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
    raise StructuredOutputError(f"{schema.__name__}: no valid output after {max_attempts} attempts") from last_error


def resolved_models(settings: Settings) -> list[dict]:
    """The model and server each role calls, after profiles and explicit settings. No secrets."""
    rows = []
    for role in ("agent", "extraction", "router"):
        endpoint = settings.endpoint(role)
        rows.append({"role": role, "model": endpoint.model, "base_url": endpoint.base_url,
                     "reasoning_effort": endpoint.reasoning_effort})
    if settings.judge_model:
        endpoint = settings.endpoint("judge")
        rows.append({"role": "judge", "model": endpoint.model, "base_url": endpoint.base_url,
                     "reasoning_effort": endpoint.reasoning_effort})
    else:
        rows.append({"role": "judge", "model": None, "note": "off: nexo eval skips generation scores"})
    if settings.embedding_provider == "fake":
        rows.append({"role": "embedding", "model": None, "note": "fake: deterministic offline vectors"})
    else:
        rows.append({"role": "embedding", "model": settings.embedding_model, "base_url": settings.embedding_base_url})
    return rows


JSON_ROLES = ("extraction", "router", "judge")
_PROBE_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
                 "additionalProperties": False}


def check_model_servers(settings: Settings, rows: list[dict], *, timeout_s: float = 5.0) -> list[dict]:
    """Mark each row with whether its server answers and lists its model (GET /models, OpenAI protocol).

    JSON roles also get one tiny schema-constrained request: a server can list a model and still be
    unable to constrain its output (e.g. Ollama MLX builds), which breaks extraction and routing.
    """
    listings: dict[str, set[str] | str] = {}
    probes: dict[tuple, str | None] = {}
    for row in rows:
        if not row.get("model"):
            continue
        key = settings.embedding_api_key if row["role"] == "embedding" else settings.endpoint(row["role"]).api_key
        if row["base_url"] not in listings:
            listings[row["base_url"]] = _served_models(row["base_url"], key.get_secret_value(), timeout_s)
        served = listings[row["base_url"]]
        if isinstance(served, str):
            row["check"] = f"unreachable: {served}"
        elif row["model"] in served or f"{row['model']}:latest" in served:  # Ollama tags untagged pulls :latest
            row["check"] = "ok"
        else:
            row["check"] = "model not served here"
            row["served"] = sorted(served)
        if row["check"] == "ok" and row["role"] in JSON_ROLES:
            probe = (row["base_url"], row["model"], row.get("reasoning_effort"))
            if probe not in probes:
                probes[probe] = _probe_structured_output(*probe, key.get_secret_value(), settings.llm_timeout_s)
            if probes[probe]:
                row["check"] = "structured output failed"
                row["detail"] = probes[probe]
    return rows


def _probe_structured_output(base_url: str, model: str, reasoning_effort: str | None, api_key: str,
                             timeout_s: float) -> str | None:
    """None when the server returns schema-valid JSON for a minimal request, else what went wrong."""
    body = {"model": model, "temperature": 0, "max_tokens": 64,
            "messages": [{"role": "user", "content": 'Reply with the JSON object {"ok": true}.'}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "probe", "schema": _PROBE_SCHEMA}}}
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
    request = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
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


def _served_models(base_url: str, api_key: str, timeout_s: float) -> set[str] | str:
    request = urllib.request.Request(base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return {entry.get("id") for entry in json.load(response).get("data", [])}
    except (OSError, ValueError) as exc:  # URLError/HTTPError/timeouts are OSError; bad JSON is ValueError
        return f"{type(exc).__name__}: {exc}"
