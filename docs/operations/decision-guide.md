---
type: Decision Guide
title: Choosing a setup
description: Decision matrices for running Nexo - which preset for your hardware, one machine or two, how models are served, which observability add-ons, memory per combination, flags versus nexo.yaml, how to run the services, whether telemetry may hold content and where secrets live.
tags: [operations, setup, decisions, observability, models]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-20T17:00:00Z }
stale_after: 2027-01-20T00:00:00Z
sources:
  - id: profiles
    resource: ../../src/nexo/local_profiles.py
    title: Hardware profile catalog (models and sizes per tier)
  - id: components
    resource: ../../src/nexo/setup/components.py
    title: Components, secrets and measured memory
  - id: local-setup
    resource: local-setup.md
    title: How local setup works
  - id: observability
    resource: observability.md
    title: Observability
  - id: adr-0015
    resource: ../decisions/adr-0015-signoz-default-observability.md
    title: "ADR-0015: SigNoz as the default observability backend"
  - id: adr-0017
    resource: ../decisions/adr-0017-secrets-from-a-provider.md
    title: "ADR-0017: Secrets from a provider, not a file"
  - id: adr-0016
    resource: ../decisions/adr-0016-declarative-setup-file.md
    title: "ADR-0016: A declarative setup file"
---

# How to read this page

Each table is one decision. Pick a row, then set it in `nexo.yaml` (`make env PRESET=...` writes a
starting point) and run `make up`; `make check` tells you whether it fits the machine.[^local-setup]
Model sizes come from the profile catalog;[^profiles] container memory was measured with
`podman stats` on a 24 GB M5 Pro after an ingest.[^components]

# 1. Which preset for your hardware

The profile's memory picks a tier: the largest one that fits. The same tiers apply to Apple Silicon
(unified memory), a Linux GPU (VRAM) and a CPU (RAM).

| You have | Preset, profile | Agent and extraction model | Judge (`nexo eval`) | Context | Model weights | Choose it when |
|---|---|---|---|---|---|---|
| Mac, 16 GB | `mac-single`, `m1-pro-16` | qwen3.5 9B | off | 8k | 7.8 GB | Trying Nexo; small documents |
| Mac, 24 GB | `mac-single`, `m5-pro-24` | qwen3.5 9B | off | 16k | 7.8 GB | Everyday use on one laptop |
| Mac or GPU, 32 GB | `mac-single` / `linux-gpu`, `m2-max-32` / `gpu-32` | qwen3 30B-A3B (MoE) | off | 16k | 20.2 GB | Better extraction and answers than 9B |
| Mac or GPU, 48 GB | `mac-single` / `linux-gpu`, `m3-max-48` / `gpu-48` | qwen3 30B-A3B | on | 32k | 27.6 GB | Long documents; evaluation runs |
| Mac, 64 GB or more | `mac-single`, `m1-max-64` | qwen3.6 35B-A3B (MoE) | on | 32k | 32.6 GB | The best local quality on one machine |
| Linux, CPU only | `linux-cpu`, `cpu-64` | by RAM, as above | by tier | by tier | by tier | No GPU; expect a few tokens per second |
| Linux, 4x 80 GB NVIDIA | `linux-vllm` | Llama 3.3 70B (vLLM) | off until `models.roles.judge` is set | 32k | on the GPUs | A shared server; many users; the production path |

`python3 src/nexo/local_profiles.py plan <profile>` prints the exact models. An explicit
`models.roles` entry in `nexo.yaml` overrides any role, e.g. a larger agent model on a 24 GB Mac.

# 2. One machine or two

| | One machine (`mac-single`) | Two machines (`mac-two-machines`) |
|---|---|---|
| Agent and extraction | This machine's tier (9B on 24 GB) | The bigger machine's tier (35B-A3B on a 64 GB Mac) |
| This machine serves | Every role | Router 4B, judge 8B, embeddings: 8.6 GB on a 24 GB Mac |
| Judge for `nexo eval` | Only from 48 GB | Yes, on the light machine |
| Extra steps | None | `ollama-setup.sh <profile> heavy` on the big machine; an SSH tunnel kept open |
| Fails when | The machine is out of memory | The tunnel or the big machine is down (`make check` names port 11435) |
| Choose it when | One machine has the memory you need | The small machine is where you work and a bigger one sits idle |

# 3. How the models are served

| `models.provider` | Runs on | Models | Concurrency | Data leaves the machine | Choose it when |
|---|---|---|---|---|---|
| `ollama` (default) | The host, natively (Metal, CUDA, ROCm or CPU) | The profile's GGUF models, pulled by `make up` | 1-2 parallel requests | No | Laptops and single GPUs |
| `vllm` | Containers on NVIDIA GPUs | `models.vllm.model` (Llama 3.3 70B) and bge-m3 | High (continuous batching) | No | A GPU server shared by several users |
| `openai-compatible` | Anything with an OpenAI API and tool calling | Whatever `models.roles` names | The server's | Depends on the server | You already run a model server, or use a hosted one cleared for your data |

The JSON roles need structured output: `make check` flags a server that cannot constrain output to
a schema (e.g. Ollama MLX builds).

# 4. Which observability to turn on

All of them receive the same OpenTelemetry data from one collector, after message content is
stripped, so turning one on or off never changes Nexo itself.[^observability]

| | SigNoz | Host metrics | Langfuse | Jaeger | LangSmith |
|---|---|---|---|---|---|
| Default | Always on (core) | On | Off | Off | Off |
| Answers | Is it healthy, where did time go: traces, service map, metrics, logs, alerts, token use | How the machine and each container are doing | How good were the answers: datasets, experiments, scores, annotation queues | Span timings only, in a minimal UI | LangChain's hosted traces and evaluators |
| Data stays on this machine | Yes | Yes | Yes | Yes | No: a cloud service (content stripped first) |
| Extra memory | 1.2-1.4 GB | - (inside the collector) | 3.0 GB | 0.01 GB | - |
| Cost and license | Free, open source (MIT) | Free | Free, open source (MIT core) | Free, open source (Apache-2.0) | Account required; free tier, paid plans |
| Needs | Secrets from `make env` | Root in the collector container, the engine's socket | `make env` secrets (made when it is on) | Nothing | `LANGSMITH_API_KEY` in `.env` |
| Turn on | - | `addons.hostMetrics` | `addons.langfuse` or `LANGFUSE=1` | `addons.jaeger` or `JAEGER=1` | `addons.langsmith` or `LANGSMITH=1` |
| Choose it when | Always: one place for operations | You want the infrastructure view; turn off to avoid a root container | You are tuning prompts or models and want to score answers | You want a light span view; SigNoz already shows spans | Your team already uses LangSmith, and the data may leave the machine |

Langfuse and SigNoz overlap only on traces: Langfuse judges answer quality, SigNoz watches the
system ([ADR-0015](../decisions/adr-0015-signoz-default-observability.md)).[^adr-0015]

# 5. Memory for each combination

Containers, measured; models are the 24 GB profile's (6.5 GB resident). The Podman VM's default
limit is 8 GiB. `make check` and `make up` stop when the setup would not fit this machine's real
memory (85%) or the Podman VM (90%); `FORCE=1` goes ahead anyway.

| Turned on | Containers | With the models (24 GB Mac) | Fits the default Podman VM |
|---|---|---|---|
| Default: Neo4j, collector, SigNoz, host metrics | ~1.9 GB | ~8.4 GB | Yes |
| + Jaeger | ~1.9 GB | ~8.4 GB | Yes |
| + Langfuse | ~4.9 GB | ~11.4 GB | Yes |
| + Langfuse + Jaeger (+ LangSmith: no local memory) | ~5.1 GB | ~11.6 GB | Yes, with ~3 GB to spare |

On a 16 GB Mac, the default stack plus the models needs about 8.4 GB; Langfuse adds 3 GB, which
leaves little for macOS and your apps, so run it on a bigger machine or only when you need it.

# 6. Keep it on, or turn it on for one run

| | `addons:` in `nexo.yaml` | `make up LANGFUSE=1` (or `JAEGER=1`, `LANGSMITH=1`) |
|---|---|---|
| Lasts | Until you change the file | This run; the next `make up` without it leaves it out |
| Recorded in `nexo.lock` | Yes | No |
| Choose it when | It is part of how you work | You want to try it, or need it for one investigation |

Both end in the same rendered stack; the flags exist so trying an add-on never needs an edit.[^adr-0016]

# 7. How to run the services

| | `make up` (`nexo setup`) | `podman compose up -d` | Native (no containers) |
|---|---|---|---|
| Uses | `nexo.yaml`: any preset, add-ons, patches | The committed `docker-compose.yml` (the `mac-single` preset) | Neo4j from Homebrew or a package |
| Models | Started for you (`ollama-setup.sh`) | Start them yourself: `make models` | Start them yourself: `make models` |
| Checks | Preflight: engine, secrets, memory | None | `nexo models --check` |
| Observability | Everything in section 4 | SigNoz and host metrics | None, or the collector and SigNoz from `make render` |
| Choose it when | Almost always | You want plain Compose, or to read the stack on GitHub | You cannot run containers |

# 8. Message content in telemetry

| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | Spans hold | Choose it when |
|---|---|---|
| `no_content` (default) | Model, tokens, timings; no prompts or answers | Always, unless the next row applies |
| `span_only` or `span_and_event` | Prompts and answers too, and `result` payloads in exported logs | Every backend that receives them is cleared for the data (PII, PHI) |

The collector also deletes content attributes and events before export, as a second line of defence.

# 9. Where secrets live

Set with `spec.secrets.provider` in `nexo.yaml`. None of them copies a secrets file between machines.[^adr-0017]

| | `file` (default) | `file` with a mounted `path` | `keychain` (opt in) | `sops` (opt in) | `vault` (opt in) |
|---|---|---|---|---|---|
| Stored in | `~/.config/nexo/config`: a `[stack]` section, owner-only | A file or per-key directory your platform mounts (Kubernetes Secret volume, Secrets Store CSI driver, a file CI writes) | The OS keychain (macOS Keychain, Linux Secret Service) | A SOPS-encrypted file in the checkout (age, PGP or cloud KMS keys) | HashiCorp Vault or OpenBao (KV v2) |
| `make env` generates | Yes | No: read-only; it names what is missing | Yes | Yes, encrypted | Yes |
| Several checkouts, one machine | Share the file | Share the mount | Share the entries | Share the file | Share the secret |
| Several machines | Each machine makes its own; nothing is copied | Each mounts from your secret manager | Each machine's own keychain | The encrypted file travels with the repo; keys stay with people | One Vault for all |
| Reaches Nexo as | In-process reads | In-process reads, never environment variables | In-process reads | In-process reads | In-process reads |
| Needs | Nothing | A mount | A desktop keychain and the `keychain` extra (`make install-keychain`) | `sops` and a `.sops.yaml` creation rule | `VAULT_ADDR`, `VAULT_TOKEN` |
| Choose it when | One machine (the usual case) | A cluster or CI | You prefer the OS keychain | A team sharing one setup through git | You already run Vault or OpenBao |

On a cluster, mount secrets as files and point `secrets.file.path` (or `NEXO_SECRETS_FILE`) at them;
never inject them as environment variables (`secretKeyRef`, `envFrom`). On your own machine `nexo setup`
passes them to compose through the environment, which stays on that machine.

[^profiles]: Hardware profile catalog (models and sizes per tier)
[^components]: Components, secrets and measured memory
[^local-setup]: How local setup works
[^observability]: Observability
[^adr-0015]: "ADR-0015: SigNoz as the default observability backend"
[^adr-0016]: "ADR-0016: A declarative setup file"
[^adr-0017]: "ADR-0017: Secrets from a provider, not a file"
