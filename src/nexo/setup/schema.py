"""The setup file (`nexo.yaml`): one declarative description of a Nexo deployment.

Shaped like SigNoz foundry's casting: a deployment target, the model servers,
core components and opt-in add-ons with defaults built in, and JSON Patch
operations on the rendered files as the escape hatch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from nexo.local_profiles import parse_profile

API_VERSION = "nexo/v1alpha1"
CORE = ("neo4j", "collector", "signoz")
ADDONS = ("langfuse", "jaeger", "langsmith", "hostMetrics", "agentInbox")
ROLES = ("agent", "extraction", "router", "judge", "embedding")
Port = Annotated[int, Field(ge=1, le=65535)]


class SetupError(ValueError):
    """The setup file, its lock or the machine is not usable; the message says why."""


class _Strict(BaseModel):
    # strict: "yes" is not a boolean and "11435" is not a port; YAML gives real types.
    model_config = ConfigDict(extra="forbid", strict=True)


class Target(_Strict):
    """Where Nexo's services run."""

    # compose: Neo4j and telemetry in containers. native: Neo4j already runs on this machine
    # (Homebrew, a package), nothing in containers, no telemetry export. Kubernetes comes later.
    mode: Literal["compose", "native"] = "compose"
    engine: Literal["auto", "podman", "docker"] = "auto"
    host: Literal["auto", "macos", "linux"] = "auto"


class Machine(_Strict):
    """A machine that serves models with Ollama, named by its hardware profile."""

    profile: str
    port: Port = 11434

    @model_validator(mode="after")
    def _known_profile(self) -> Machine:
        parse_profile(self.profile)  # raises ValueError naming the accepted forms
        return self


class Vllm(_Strict):
    """vLLM on NVIDIA GPUs, served by the compose stack."""

    gpus: int = Field(default=4, ge=1)
    model: str = "meta-llama/Llama-3.3-70B-Instruct"
    embedding: str = "BAAI/bge-m3"


class Role(_Strict):
    """An explicit model (and server) for one role; wins over the profiles."""

    model: str | None = None
    base_url: str | None = Field(default=None, alias="baseUrl")


class Models(_Strict):
    """Where the OpenAI-compatible model servers are."""

    provider: Literal["ollama", "vllm", "openai-compatible"] = "ollama"
    local: Machine | None = None
    remote: Machine | None = None
    vllm: Vllm | None = None
    roles: dict[Literal[ROLES], Role] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _matches_provider(self) -> Models:
        if self.provider == "ollama" and self.local is None:
            raise ValueError("provider ollama needs models.local.profile")
        if self.remote is not None and self.local is None:
            raise ValueError(
                "models.remote needs models.local (the machine running Nexo)"
            )
        if self.remote is not None and self.remote.port == 11434:
            self.remote.port = 11435  # the SSH tunnel's local end
        if self.provider == "vllm" and self.vllm is None:
            self.vllm = Vllm()
        if self.provider == "openai-compatible" and not self.roles:
            raise ValueError("provider openai-compatible needs models.roles")
        return self


class Component(_Strict):
    """Overrides for one component; every field is optional."""

    enabled: bool | None = None
    image: str | None = None  # the component's main service
    images: dict[str, str] = Field(default_factory=dict)  # service name -> image
    env: dict[str, str] = Field(default_factory=dict)  # the main service's environment
    config: dict[str, Any] = Field(default_factory=dict)  # merged into its config file
    project: str | None = None  # langsmith only


class Telemetry(_Strict):
    """What Nexo itself sends to the collector."""

    logs: Literal["otlp", "none"] = "otlp"
    # The question's text on traces (self-hosted backends only; LangSmith never gets it).
    query_text: bool = Field(default=False, alias="queryText")
    # The most sensitive run whose question and answer are logged: none, public, internal,
    # pii or phi (TELEMETRY_CONTENT_MAX_SENSITIVITY).
    content: Literal["none", "public", "internal", "pii", "phi"] = "internal"


class FileSecrets(_Strict):
    """A secrets file or a directory of per-key files mounted by your platform (read-only)."""

    path: str | None = None  # default: ~/.config/nexo/config, written by nexo setup


class Sops(_Strict):
    """A SOPS-encrypted dotenv file, relative to the checkout; keys from .sops.yaml rules."""

    file: str = "secrets/nexo.sops.env"


class Vault(_Strict):
    """HashiCorp Vault or OpenBao, KV version 2."""

    address: str | None = None  # default: $VAULT_ADDR
    mount: str = "secret"
    path: str | None = None  # default: nexo/<metadata.name>
    token_env: str = Field(default="VAULT_TOKEN", alias="tokenEnv")


class Secrets(_Strict):
    """Where generated secrets live: ~/.config/nexo/config unless you opt in to another."""

    provider: Literal["file", "keychain", "sops", "vault"] = "file"
    file: FileSecrets | None = None
    sops: Sops | None = None
    vault: Vault | None = None

    @model_validator(mode="after")
    def _defaults_for_provider(self) -> Secrets:
        if self.provider == "sops" and self.sops is None:
            self.sops = Sops()
        if self.provider == "vault" and self.vault is None:
            self.vault = Vault()
        return self


class Ports(_Strict):
    """Host ports the stack publishes on 127.0.0.1: change them to run a second stack."""

    neo4j_bolt: Port = Field(default=7687, alias="neo4jBolt")
    neo4j_http: Port = Field(default=7474, alias="neo4jHttp")
    otlp: Port = 4318
    metrics: Port = 8889  # the collector's Prometheus endpoint
    signoz: Port = 8080
    langfuse: Port = 3000
    langfuse_storage: Port = Field(default=9090, alias="langfuseStorage")
    jaeger: Port = 16686
    agent_inbox: Port = Field(default=3030, alias="agentInbox")
    review_inbox: Port = Field(
        default=2024, alias="reviewInbox"
    )  # the adapter, on the host
    vllm: Port = 8000
    vllm_embedding: Port = Field(default=8001, alias="vllmEmbedding")

    @property
    def bolt(self) -> str:
        """Neo4j as the host sees it."""
        return f"bolt://localhost:{self.neo4j_bolt}"


class Patch(_Strict):
    """RFC 6902 operations on one rendered file (relative to the render folder)."""

    target: str
    operations: list[dict[str, Any]]


class Spec(_Strict):
    """Everything below `spec:`."""

    target: Target = Field(default_factory=Target)
    models: Models
    components: dict[Literal[CORE], Component] = Field(default_factory=dict)
    addons: dict[Literal[ADDONS], Component] = Field(default_factory=dict)
    telemetry: Telemetry = Field(default_factory=Telemetry)
    secrets: Secrets = Field(default_factory=Secrets)
    ports: Ports = Field(default_factory=Ports)
    # Which answers wait for a person: on-findings (the default: no findings and grounded
    # citations are approved as system:auto); always; never (only warn findings are approved).
    review: Literal["always", "on-findings", "never"] = "on-findings"
    patches: list[Patch] = Field(default_factory=list)

    @model_validator(mode="after")
    def _core_stays_on(self) -> Spec:
        off = [name for name, c in self.components.items() if c.enabled is False]
        if off:
            raise ValueError(f"core components cannot be disabled yet: {off}")
        return self

    @model_validator(mode="after")
    def _native_has_no_containers(self) -> Spec:
        if self.target.mode != "native":
            return self
        problems = [f"addons.{n}" for n, a in self.addons.items() if a.enabled]
        if self.models.provider == "vllm":
            problems.append("models.provider vllm")
        if self.patches:
            problems.append("patches")
        if problems:
            raise ValueError(
                f"target.mode native runs no containers: remove {', '.join(problems)}"
                " or use target.mode compose"
            )
        return self

    @model_validator(mode="after")
    def _ports_distinct(self) -> Spec:
        taken = {
            f"ports.{name}": port
            for name, port in self.ports.model_dump(by_alias=True).items()
        }
        for name in ("local", "remote"):
            if (machine := getattr(self.models, name)) is not None:
                taken[f"models.{name}.port"] = machine.port
        first: dict[int, str] = {}
        clashes = []
        for where, port in taken.items():
            if port in first:
                clashes.append(f"{first[port]} and {where} are both {port}")
            first.setdefault(port, where)
        if clashes:
            raise ValueError("; ".join(clashes))
        return self


class Metadata(_Strict):
    """The deployment's name: the compose project, so it prefixes volume names."""

    name: str = Field(default="nexo", pattern=r"^[a-z0-9][a-z0-9_-]*$")


class Casting(_Strict):
    """A whole `nexo.yaml`."""

    api_version: Literal[API_VERSION] = Field(alias="apiVersion")
    kind: Literal["Setup"]
    metadata: Metadata = Field(default_factory=Metadata)
    spec: Spec


def load_casting(path: Path) -> Casting:
    """Read and validate a setup file; errors name the file and the field."""
    if not path.exists():
        raise SetupError(f"{path} not found; create one with `nexo setup init`")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SetupError(f"{path}: not valid YAML: {exc}") from exc
    if data is None:
        raise SetupError(f"{path} is empty; start from a preset: nexo setup init")
    if not isinstance(data, dict):
        raise SetupError(f"{path}: expected apiVersion, kind and spec at the top level")
    try:
        return Casting.model_validate(data)
    except ValidationError as exc:
        raise SetupError(f"{path}: {describe(exc)}") from exc


def describe(exc: ValidationError) -> str:
    """One `field.path: message` per problem, without pydantic's documentation links."""
    problems = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error["loc"]) or "top level"
        problems.append(f"{where}: {error['msg'].removeprefix('Value error, ')}")
    return "; ".join(problems)
