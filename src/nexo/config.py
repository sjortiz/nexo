"""Runtime configuration, loaded from environment variables (or `.env`).

Every infrastructure endpoint is configurable so the same code runs against
in-cluster services (`vllm-inference-service`, `neo4j`) and local fakes.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from nexo.local_profiles import needs_reasoning_off, plan
from nexo.secret_store import SecretStoreError, parse_dotenv, store_from_options

ModelRole = Literal["agent", "extraction", "router", "judge"]
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high"]


@dataclass(frozen=True)
class ModelEndpoint:
    """Resolved connection settings for one model role."""

    base_url: str
    api_key: SecretStr
    model: str
    reasoning_effort: ReasoningEffort | None = None  # JSON roles only


log = logging.getLogger(__name__)
RENDERED_ENV = ".nexo/render/nexo.env"
# Settings that `nexo setup` generates and keeps in the secrets provider.
STORED_SECRETS = (
    "NEO4J_PASSWORD",
    "VLLM_API_KEY",
    "EMBEDDING_API_KEY",
    "QUERY_HASH_KEY",
)


class _SecretStoreSource(PydanticBaseSettingsSource):
    """Generated secrets, read in-process from the provider nexo.env names (a file by default)."""

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        rendered = Path(RENDERED_ENV)
        options = (
            parse_dotenv(rendered.read_text(encoding="utf-8"))
            if rendered.exists()
            else {}
        )
        options |= {k: v for k, v in os.environ.items() if k.startswith("NEXO_")}
        if "NEXO_SECRETS_PROVIDER" not in options:
            return {}  # not set up by `nexo setup`: the environment and .env only
        try:
            values = store_from_options(options).get(STORED_SECRETS)
        except SecretStoreError as exc:
            log.warning("secrets: %s", exc)
            return {}
        return {key.lower(): value for key, value in values.items()}


class Settings(BaseSettings):
    """All runtime settings, read from the environment, the secrets provider or `.env`."""

    # First match wins: the environment, generated secrets from the provider `nexo setup`
    # chose (~/.config/nexo/config by default), .nexo/render/nexo.env, then .env.
    model_config = SettingsConfigDict(
        env_file=(".env", RENDERED_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources = [init_settings, env_settings]
        # An explicit _env_file (tests, tools) means exactly those files, no provider.
        if getattr(dotenv_settings, "env_file", None) == cls.model_config["env_file"]:
            sources.append(_SecretStoreSource(settings_cls))
        return (*sources, dotenv_settings, file_secret_settings)

    # --- Default chat model (any OpenAI-compatible server: vLLM, Ollama, ...) --
    vllm_base_url: str = "http://vllm-inference-service:8000/v1"
    vllm_api_key: SecretStr = SecretStr("EMPTY")
    vllm_model: str = "meta-llama/Llama-3.3-70B-Instruct"

    # --- Per-role overrides ---------------------------------------------------
    # Each role falls back to the default above. Point roles at different
    # models or hosts, e.g. the agent on a large model on one machine and
    # extraction/routing on a smaller one elsewhere.
    agent_base_url: str | None = None  # Strands investigator (multi-turn tool calls)
    agent_api_key: SecretStr | None = None
    agent_model: str | None = None
    extraction_base_url: str | None = None  # triple extraction + entity resolution
    extraction_api_key: SecretStr | None = None
    extraction_model: str | None = None
    router_base_url: str | None = None  # intake validation
    router_api_key: SecretStr | None = None
    router_model: str | None = None
    # Thinking models (e.g. Qwen3.5/3.6) apply JSON-schema constraints only after
    # their reasoning; set "none" on a JSON role served by one. Unset = the
    # parameter is not sent (non-thinking models may reject it). There is no
    # agent setting: the agent keeps its reasoning.
    extraction_reasoning_effort: ReasoningEffort | None = None
    router_reasoning_effort: ReasoningEffort | None = None
    judge_reasoning_effort: ReasoningEffort | None = None
    llm_timeout_s: float = 120.0
    llm_max_retries: int = Field(default=3, ge=0)
    agent_temperature: float = 0.1
    agent_max_tokens: int = 2048

    # --- Embeddings (a separate OpenAI-compatible pooling server) ------------
    # A 70B generative deployment should not also serve embeddings; run a
    # dedicated embedding model. `fake` is deterministic and for local/tests.
    embedding_provider: Literal["openai_compatible", "fake"] = "openai_compatible"
    embedding_base_url: str = "http://vllm-embedding-service:8000/v1"
    embedding_api_key: SecretStr = SecretStr("EMPTY")
    embedding_model: str = "BAAI/bge-m3"
    embedding_dimensions: int = 1024

    # --- Neo4j --------------------------------------------------------------
    neo4j_uri: str = "neo4j://neo4j:7687"
    neo4j_database: str = "neo4j"
    neo4j_user: str = "neo4j"
    neo4j_password: SecretStr = SecretStr("")
    # The agent's Cypher tool should use a reader-role account (Enterprise).
    # Falls back to the writer account; READ access mode still applies.
    neo4j_read_user: str | None = None
    neo4j_read_password: SecretStr | None = None
    neo4j_query_timeout_s: float = 15.0
    neo4j_max_rows: int = Field(default=50, ge=1, le=1000)

    # --- Chunk store and per-stack data (ADR-0018) ------------------------------
    # neo4j: chunk text and embeddings on the graph's (:Chunk) nodes, so every checkout of
    # a stack sees the same chunks; local: an in-process JSON file (offline runs, tests).
    chunk_store: Literal["neo4j", "local"] = "neo4j"
    # The local store's file; also what `nexo chunks import` reads by default.
    vector_store_path: Path = Path(".data/vector_store.json")
    # The stack (nexo.yaml metadata.name, rendered as NEXO_STACK): names its data directory.
    nexo_stack: str = "nexo"
    # Seed search: BM25 + vector fused with RRF (hybrid), or either alone.
    retrieval_mode: Literal["hybrid", "vector", "keyword"] = "hybrid"

    # --- Ingestion ------------------------------------------------------------
    chunk_size: int = 1200
    chunk_overlap: int = 150
    extraction_attempts: int = Field(default=3, ge=1)
    extraction_concurrency: int = Field(default=8, ge=1)
    require_grounded_evidence: bool = True
    # Tail of the previous chunk shown to the extractor as context (not evidence),
    # so references like "the company" resolve across chunk boundaries.
    extraction_context_chars: int = Field(default=500, ge=0)
    # Relation ontology (see deploy/ontology.example.json). Unset = free-form predicates.
    ontology_path: Path | None = None
    # off: exact keys only; deterministic: + alias matches; llm: + model arbitration.
    entity_resolution: Literal["off", "deterministic", "llm"] = "llm"
    resolution_max_candidates: int = Field(default=25, ge=1)
    resolution_batch_size: int = Field(default=30, ge=1)
    # JSON object of acronym -> expansion, appended to embedded chunk text.
    glossary_path: Path | None = None

    # --- Sensitive data (PII/PHI) ---------------------------------------------
    # comprehend_medical sends chunk text to Amazon Comprehend Medical
    # DetectPHI; enable only where that data flow is approved.
    sensitive_data_provider: Literal["none", "regex", "comprehend_medical"] = "regex"
    sensitive_data_policy: Literal["tag", "redact"] = "tag"
    aws_region: str | None = None
    # Highest sensitivity a caller may see when the request does not say.
    default_clearance: Literal["public", "internal", "pii", "phi"] = "internal"
    # Clearance of the MCP server (`nexo mcp`); one per server, since MCP over
    # stdio has no per-user identity. Unset = DEFAULT_CLEARANCE.
    mcp_clearance: Literal["public", "internal", "pii", "phi"] | None = None

    # --- Investigator agent budget -------------------------------------------
    agent_max_turns: int = Field(default=12, ge=1)
    agent_max_total_tokens: int = Field(default=120_000, ge=1)
    agent_max_tool_calls: int = Field(default=16, ge=1)
    agent_timeout_s: float = 300.0
    agent_max_chunks_per_call: int = Field(default=10, ge=1)
    seed_chunks: int = Field(
        default=5,
        ge=0,
        description="vector hits handed to the agent for vector/hybrid intents",
    )
    tool_result_max_chars: int = 12_000

    # --- Orchestration ---------------------------------------------------------
    max_query_chars: int = 2000
    checkpoint_backend: Literal["sqlite", "postgres", "memory"] = "sqlite"
    # Default: <data dir>/<stack>/checkpoints.sqlite, shared by every checkout of the stack.
    checkpoint_sqlite_path: Path | None = None
    checkpoint_postgres_uri: SecretStr | None = None

    # --- Evaluation (LLM-as-a-judge) ------------------------------------------
    # Prefer a different model from the generator to reduce self-preference bias.
    # Judging is off unless JUDGE_MODEL is set.
    judge_base_url: str | None = None
    judge_api_key: SecretStr | None = None
    judge_model: str | None = None

    # --- Observability ---------------------------------------------------------
    otel_service_name: str = "nexo"
    # OTLP/HTTP base URL, e.g. http://otel-collector:4318. Unset = no export.
    otel_exporter_otlp_endpoint: str | None = None
    otel_console_export: bool = False
    # Opt-in: also send log records over OTLP (to the collector) with their trace ids.
    # Standard OTel variable name; Nexo defaults it to "none".
    otel_logs_exporter: Literal["none", "otlp"] = "none"
    # Standard GenAI content-capture switch, shared with the OpenTelemetry GenAI
    # instrumentations. Prompts, answers and Cypher can carry PII: keep them off
    # spans unless the trace backend is cleared for that data.
    otel_instrumentation_genai_capture_message_content: Literal[
        "no_content", "span_only", "event_only", "span_and_event"
    ] = "no_content"
    # Strands reads this: latest GenAI conventions, and redact content attributes
    # (an empty unredacted list) unless content capture is enabled.
    otel_semconv_stability_opt_in: str | None = None
    # Instrument the openai SDK (vLLM calls from LangChain and Strands) with the
    # official GenAI instrumentation: one `chat {model}` CLIENT span per call.
    otel_instrument_openai: bool = True
    deployment_environment: str = "dev"
    # Which user and which question a trace belongs to (ADR-0009, amended). user.id is
    # NEXO_USER, else the login name; the question's length and a keyed hash (the key is
    # generated into the secrets store, never exported) are always on the request span;
    # its text only with TRACE_QUERY_TEXT=true (telemetry.queryText in nexo.yaml).
    nexo_user: str | None = None
    query_hash_key: SecretStr | None = None
    trace_query_text: bool = False

    # --- Agent Inbox add-on (nexo review-inbox; both bind 127.0.0.1) ----------------
    review_inbox_port: int = Field(default=2024, ge=1, le=65535)  # the adapter
    agent_inbox_port: int = Field(default=3030, ge=1, le=65535)  # the inbox's own page

    # --- Logging -----------------------------------------------------------------
    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"

    # --- Local hardware profiles (Ollama: Apple Silicon, Linux GPU or CPU) -------
    # LOCAL_PROFILE alone: this Mac serves every role. With REMOTE_PROFILE, the
    # remote Mac serves the agent and extraction models (through REMOTE_BASE_URL,
    # usually an SSH tunnel) and this Mac serves router, judge and embeddings.
    # Any role setting given explicitly wins over the profile. See local_profiles.py.
    local_profile: str | None = None
    local_base_url: str = "http://localhost:11434/v1"
    remote_profile: str | None = None
    remote_base_url: str = "http://localhost:11435/v1"

    @field_validator("local_profile", "remote_profile", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: str | None) -> str | None:
        # `nexo setup` writes REMOTE_PROFILE= to override a stale .env; blank means unset.
        return value or None

    @model_validator(mode="after")
    def _per_stack_paths(self) -> Settings:
        if self.checkpoint_sqlite_path is None:
            self.checkpoint_sqlite_path = (
                data_dir() / self.nexo_stack / "checkpoints.sqlite"
            )
        return self

    @model_validator(mode="after")
    def _apply_local_profiles(self) -> Settings:
        if not self.local_profile:
            if self.remote_profile:
                raise ValueError(
                    "REMOTE_PROFILE needs LOCAL_PROFILE (the Mac that runs Nexo)"
                )
            return self
        explicit = set(self.model_fields_set)
        if self.remote_profile:
            heavy, light = plan(self.remote_profile, "heavy"), plan(
                self.local_profile, "light"
            )
            placement = {
                role: (heavy.roles[role], self.remote_base_url)
                for role in ("agent", "extraction")
            }
            placement |= {
                role: (light.roles[role], self.local_base_url)
                for role in ("router", "judge", "embedding")
            }
            parallel = heavy.num_parallel
        else:
            single = plan(self.local_profile, "single")
            placement = {
                role: (model, self.local_base_url)
                for role, model in single.roles.items()
            }
            parallel = single.num_parallel

        def default(field: str, value: object) -> None:
            if field not in explicit:
                setattr(self, field, value)

        for role in ("agent", "extraction", "router", "judge"):
            model, base_url = placement[role]
            # Role disabled by the profile (e.g. no judge on a small Mac), unless a
            # model is set for it: that one runs on the profile's server too.
            if model is None:
                if f"{role}_model" not in explicit:
                    continue
                model = getattr(self, f"{role}_model")
            default(f"{role}_model", model)
            default(f"{role}_base_url", base_url)
            # Ollama ignores the key; clients need one.
            default(f"{role}_api_key", SecretStr("ollama"))
            if role != "agent" and needs_reasoning_off(model):
                default(f"{role}_reasoning_effort", "none")
        embedding_model, embedding_url = placement["embedding"]
        default("embedding_provider", "openai_compatible")
        default("embedding_model", embedding_model)
        default("embedding_base_url", embedding_url)
        default("embedding_api_key", SecretStr("ollama"))
        default("embedding_dimensions", 1024)
        # Local models are slower than a GPU server.
        default("llm_timeout_s", 300.0)
        default("agent_timeout_s", 900.0)
        default("extraction_concurrency", parallel)
        return self

    def endpoint(self, role: ModelRole) -> ModelEndpoint:
        """Model endpoint for a role, falling back to the VLLM_* defaults field by field."""
        return ModelEndpoint(
            base_url=getattr(self, f"{role}_base_url") or self.vllm_base_url,
            api_key=getattr(self, f"{role}_api_key") or self.vllm_api_key,
            model=getattr(self, f"{role}_model") or self.vllm_model,
            reasoning_effort=getattr(self, f"{role}_reasoning_effort", None),
        )

    @property
    def capture_content(self) -> bool:
        """Whether GenAI spans may record prompt and completion content."""
        return self.otel_instrumentation_genai_capture_message_content in {
            "span_only",
            "span_and_event",
        }


def data_dir() -> Path:
    """Per-user Nexo data: NEXO_DATA_DIR, $XDG_DATA_HOME/nexo or ~/.local/share/nexo."""
    if explicit := os.environ.get("NEXO_DATA_DIR"):
        return Path(explicit)
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "nexo"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide cached settings."""
    return Settings()
