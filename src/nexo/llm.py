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
    if settings.structured_reasoning_effort and role != "agent":
        extra["reasoning_effort"] = settings.structured_reasoning_effort
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
