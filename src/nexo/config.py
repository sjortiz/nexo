"""Runtime configuration, loaded from environment variables (or `.env`).

Every infrastructure endpoint is configurable so the same code runs against
in-cluster services (`vllm-inference-service`, `neo4j`) and local fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ModelRole = Literal["agent", "extraction", "router", "judge"]


@dataclass(frozen=True)
class ModelEndpoint:
    base_url: str
    api_key: SecretStr
    model: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

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
    # their reasoning; "none" turns reasoning off for the JSON roles (router,
    # extraction, judge). Unset = parameter not sent. The agent is unaffected.
    structured_reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] | None = None
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

    # --- Vector store (local index, persisted between processes) -------------
    vector_store_path: Path = Path(".data/vector_store.json")

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

    # --- Investigator agent budget -------------------------------------------
    agent_max_turns: int = Field(default=12, ge=1)
    agent_max_total_tokens: int = Field(default=120_000, ge=1)
    agent_max_tool_calls: int = Field(default=16, ge=1)
    agent_timeout_s: float = 300.0
    agent_max_chunks_per_call: int = Field(default=10, ge=1)
    seed_chunks: int = Field(default=5, ge=0, description="vector hits handed to the agent for vector/hybrid intents")
    tool_result_max_chars: int = 12_000

    # --- Orchestration ---------------------------------------------------------
    max_query_chars: int = 2000
    checkpoint_backend: Literal["sqlite", "postgres", "memory"] = "sqlite"
    checkpoint_sqlite_path: Path = Path(".data/checkpoints.sqlite")
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
    # Standard GenAI content-capture switch, shared with the OpenTelemetry GenAI
    # instrumentations. Prompts, answers and Cypher can carry PII: keep them off
    # spans unless the trace backend is cleared for that data.
    otel_instrumentation_genai_capture_message_content: Literal[
        "no_content", "span_only", "event_only", "span_and_event"] = "no_content"
    # Strands reads this: latest GenAI conventions, and redact content attributes
    # (an empty unredacted list) unless content capture is enabled.
    otel_semconv_stability_opt_in: str | None = None
    # Instrument the openai SDK (vLLM calls from LangChain and Strands) with the
    # official GenAI instrumentation: one `chat {model}` CLIENT span per call.
    otel_instrument_openai: bool = True
    deployment_environment: str = "dev"

    # --- Logging -----------------------------------------------------------------
    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"

    def endpoint(self, role: ModelRole) -> ModelEndpoint:
        """Model endpoint for a role, falling back to the VLLM_* defaults field by field."""
        return ModelEndpoint(
            base_url=getattr(self, f"{role}_base_url") or self.vllm_base_url,
            api_key=getattr(self, f"{role}_api_key") or self.vllm_api_key,
            model=getattr(self, f"{role}_model") or self.vllm_model,
        )

    @property
    def capture_content(self) -> bool:
        return self.otel_instrumentation_genai_capture_message_content in {"span_only", "span_and_event"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
