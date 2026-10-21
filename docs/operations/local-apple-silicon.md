---
type: Playbook
title: Local on Apple Silicon
description: Run Nexo on one or two Apple Silicon Macs, with services in Podman or fully native, choosing models with a hardware profile such as m1-max-64.
resource: ../../src/nexo/local_profiles.py
tags: [operations, local, apple-silicon, ollama, podman]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-20T21:00:00Z }
stale_after: 2027-01-19T00:00:00Z
sources:
  - id: profiles
    resource: ../../src/nexo/local_profiles.py
    title: Hardware profile catalog
  - id: setup
    resource: ../../deploy/local/ollama-setup.sh
    title: Ollama setup script
  - id: env
    resource: ../../deploy/local/local.env.example
    title: Local Nexo environment
  - id: compose
    resource: ../../src/nexo/setup/render.py
    title: nexo setup renderer
  - id: preset
    resource: ../../examples/setups/mac-single/nexo.yaml
    title: mac-single preset
  - id: native
    resource: ../../examples/setups/mac-native/nexo.yaml
    title: mac-native preset
  - id: check
    resource: ../../src/nexo/setup/check.py
    title: nexo setup preflight
  - id: adr-0020
    resource: ../decisions/adr-0020-native-target-mode.md
    title: "ADR-0020: Native mode in the setup file"
  - id: ollama
    resource: ../references/ollama.md
    title: Ollama
  - id: live
    resource: ../../tests/test_live_models.py
    title: Live model checks
---

# Choose

How the pieces fit together (roles, the profile catalog, ports): [How local setup works](local-setup.md).
Two independent choices:

| | **Podman path** | **Native path** |
|---|---|---|
| Models | Ollama, native (Metal GPU) | Ollama, native (Metal GPU) |
| Neo4j | container (`neo4j:5.26-community`) | Homebrew (`neo4j` 2026.09) |
| Telemetry | collector (strips content) + SigNoz containers; Langfuse, Jaeger optional | the same containers, in a small Podman machine; or off |
| Nexo | container (`make app`) or `uv sync` | `uv sync` |
| Preset | `mac-single`, `mac-two-machines` | `mac-native` (`target.mode: native`) |
| Good for | matching the server setup; clean teardown | the core and models get all the memory |

| | **One Mac** | **Two Macs** |
|---|---|---|
| `.env` | `LOCAL_PROFILE=m1-max-64` | `LOCAL_PROFILE=m5-pro-24` + `REMOTE_PROFILE=m1-max-64` |
| Serves | every role on this Mac | remote Mac: agent + extraction; this Mac: router, embeddings, judge, Nexo |

Containers on macOS cannot use the GPU, so the models are native in both paths; containers reach the
host's Ollama and SSH tunnel through `host.containers.internal`, which reaches services bound to the
host's `127.0.0.1` (verified with Podman 6.1 on macOS).[^compose]

# Hardware profiles

A profile is `<chip>-<memory GB>`: `m1-pro-16`, `m5-pro-24`, `m1-max-32`, `m1-max-64`, `m2-ultra-192`.
Memory picks the largest tier that fits; the chip is informational. The setup script and Nexo read
the same catalog, and explicit settings win over it: see
[one catalog, two readers](local-setup.md#one-catalog-two-readers).[^profiles]

| Tier | One Mac (`single`) | Agent Mac (`heavy`) | Nexo Mac (`light`) | Context |
|---|---|---|---|---|
| 16 GB | `qwen3.5:9b-q4_K_M` for all chat roles, `bge-m3`, no judge (≈7.8 GB) | `qwen3.5:9b-q4_K_M` | `qwen3:4b-instruct-2507-q4_K_M`, `bge-m3`, no judge | 8K |
| 24 GB | `qwen3.5:9b-q4_K_M` for all chat roles, `bge-m3`, no judge (≈7.8 GB) | `qwen3.5:9b-q4_K_M` | `qwen3:4b-instruct-2507-q4_K_M`, `bge-m3`, `llama3.1:8b-instruct-q4_K_M` judge (≈8.6 GB) | 16K |
| 32 GB | `qwen3:30b-a3b-instruct-2507-q4_K_M` for all chat roles, `bge-m3`, no judge (≈20 GB) | `qwen3:30b-a3b-instruct-2507-q4_K_M` | as 24 GB | 16K |
| 48 GB | `qwen3:30b-a3b-instruct-2507-q4_K_M`, router `qwen3:4b-instruct-2507-q4_K_M`, `llama3.1:8b-instruct-q4_K_M`, `bge-m3` (≈28 GB) | `qwen3:30b-a3b-instruct-2507-q4_K_M` | as 24 GB | 32K |
| 64 GB | `qwen3.6:35b-a3b-q4_K_M`, router `qwen3:4b-instruct-2507-q4_K_M`, `llama3.1:8b-instruct-q4_K_M`, `bge-m3` (≈33 GB) | `qwen3.6:35b-a3b-q4_K_M` | as 24 GB | 32K |

Sizes are Ollama library download sizes; KV cache and the OS need the rest. `qwen3.5:9b-q4_K_M` and
`qwen3.6:35b-a3b-q4_K_M` are thinking models: the profile sets reasoning off for their JSON roles
(`EXTRACTION_REASONING_EFFORT=none`, ...) and leaves the agent's reasoning on. Inspect any profile with
`python3 src/nexo/local_profiles.py plan m1-max-64 heavy`.

Optional, in no tier: `gemma4:31b` (20 GB, dense, tools, thinking). First run
`ollama pull gemma4:31b` on the Mac serving agent/extraction; the profile setup does not pull it.
Then set `AGENT_MODEL=gemma4:31b` and `EXTRACTION_MODEL=gemma4:31b`
in place of `qwen3.6:35b-a3b-q4_K_M` on a 64 GB Mac; reasoning goes off for its
JSON roles too. On the golden set (4 cases, M1 Max) it scored the same as `qwen3.6:35b-a3b-q4_K_M`
with half the tool calls, at about 3× the time per question.

# Podman path

1. `brew install ollama podman` and `podman machine init` (first time only), `podman machine start`.
2. `make env PRESET=mac-single` (or `mac-two-machines`, see "Two Macs" below): writes `nexo.yaml`
   and generates `NEO4J_PASSWORD` and the SigNoz secrets into `~/.config/nexo/config` (`.env` keeps
   personal settings).[^env] It sets `models.local.profile` to this Mac's profile, read from the chip
   and memory (an M1 Max with 64 GB gets `m1-max-64`); `make env PROFILE=m5-pro-24` chooses another.[^preset]
3. `make check`: the container engine, secrets and a memory budget for the models and containers
   against this Mac and the Podman VM.
4. `make up` - pulls and preloads the profile's models and starts Ollama with the right context
   length, keep-alive and KV-cache settings, bound to `127.0.0.1`;[^setup] then Neo4j on
   `127.0.0.1:7687`, traces, metrics and logs in SigNoz at http://localhost:8080 (log in with `SIGNOZ_USER_EMAIL` /
   `SIGNOZ_USER_PASSWORD` from `~/.config/nexo/config`), raw metrics at http://localhost:8889/metrics. Infrastructure metrics
   cover the Podman VM, not the Mac. Add Langfuse (evaluation) or Jaeger with `make up LANGFUSE=1` /
   `make up JAEGER=1` for one run, or under `addons:` in `nexo.yaml` ([Observability](observability.md#backends)).
   The default stack measured about 1.9 GB of containers plus 6.5 GB of models on a 24 GB M5 Pro;
   with every add-on about 5.1 GB of containers. The Podman VM's memory depends on the Podman version
   and how the machine was created (one M1 Max's VM had 5.6 GiB): `make check` compares the containers
   with it and, when they do not fit, prints the `podman machine set --memory` value to use.[^check]
5. Run Nexo either natively (`uv sync`, then `nexo ...` in the activated `.venv`) or in a container:
   `make app ARGS="ingest /corpus/memo.md"` with documents in `./corpus`.
   The container's `LOCAL_BASE_URL`/`REMOTE_BASE_URL` already point at `host.containers.internal`.

# Native path

A bare-metal core: Ollama, Homebrew's Neo4j and Nexo (from the `.venv`) run on the Mac. Containers
are only for telemetry (the collector and SigNoz) and add-ons, which Nexo reaches on `localhost`
ports. `target.mode: native` in `nexo.yaml` declares it, so `nexo setup` stays the single entry
point and `.env` needs no edits.[^adr-0020]

1. `brew install ollama neo4j uv` (Neo4j 2026.09 Community, Java 21; Nexo's Neo4j tests pass on it),
   then `uv sync && source .venv/bin/activate` (uv installs Python 3.12).
2. `make env PRESET=mac-native`: writes `nexo.yaml` with this Mac's profile and generates
   `NEO4J_PASSWORD` into `~/.config/nexo/config`.[^native]
3. A Neo4j that has never started: `make neo4j-init` sets its initial password to the stored one;
   then `brew services start neo4j`. A Neo4j already in use keeps its password: change it to the
   stored one in `cypher-shell` (`ALTER CURRENT USER SET PASSWORD FROM '<old>' TO '<stored>'`).
4. `make up`: pulls and preloads the profile's models, renders `.nexo/render/` and runs compose for
   the container services (the collector, SigNoz and any add-ons; never Neo4j or Nexo).
   `nexo.env` has `NEO4J_URI` on `localhost`, `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`
   and `NEXO_SECRETS_*` naming the store that holds the password.[^compose]
5. `make check`: secrets, Neo4j's login with the stored password, the container engine, memory, and
   each role's model. Then `nexo ...` in the activated `.venv`.

Add-ons are opt-in under `addons:` (or `make up LANGFUSE=1`); `telemetry: { enabled: false }`
leaves no container and a blank endpoint. Patches apply to container services only, and vLLM needs
`compose`.

## Telemetry in its own Podman machine

Containers compete with the models for memory, so give them a small machine of their own. Podman
5.6 runs one machine at a time: keep the full setup in `podman-machine-default` (for example 12 GiB)
and the telemetry in a second one, rootful so the host-metrics add-on can read the container socket:

```bash
podman machine init --rootful --cpus 2 --memory 4096 --disk-size 60 nexo-telemetry
# nexo.yaml: spec.target.machine: nexo-telemetry
make up SWITCH=1      # stops the running machine, and every container on it, then starts this one
```

Minimum size for a Podman machine:

| Runs on the machine | Memory | Disk |
|---|---|---|
| Collector and SigNoz (the telemetry machine) | 3 GiB | 30 GB |
| The above, and building or running the Agent Inbox add-on | 4 GiB | 60 GB |
| Neo4j, the collector, SigNoz and Langfuse (the full container stack) | 12 GiB | 90 GB |

The containers themselves need about 1.5 GB for the collector and SigNoz; the rest is headroom for
the VM, image builds and SigNoz's ClickHouse merges. `make check` computes the exact need for the
add-ons you turned on and prints the `podman machine set --memory` value when the machine is too
small. To grow an existing machine: `podman machine stop nexo-telemetry && podman machine set
--memory 4096 --disk-size 60 nexo-telemetry && podman machine start nexo-telemetry` (the disk can
only grow).

Without `SWITCH=1`, `make up` stops with a message when another machine is running. `make check`
reports the active machine against `nexo-telemetry`, and warns when the machine's memory is far
above what the containers need (about 1.5 GB for the collector and SigNoz). On a rootless machine
the host-metrics add-on fails with `mkdir /run/podman/podman.sock: permission denied`; `check`
catches it and prints the fix: `podman machine stop nexo-telemetry && podman machine set --rootful
nexo-telemetry && podman machine start nexo-telemetry`.

Switching between paths is a change to `target.mode`; every render removes the files the other mode
wrote. If `nexo.yaml` changes after the last render, Nexo warns that `nexo.env` is stale until
`make render` (or `make up`) runs again. `nexo config --explain` prints each setting with its source
(environment, secrets store, `.nexo/render/nexo.env`, `.env`, profile or default), secrets redacted.

# Two Macs

Uses the `mac-two-machines` preset (Podman path); for a bare-metal core, set `target.mode: native` in it (its add-ons then run as containers beside the collector). Example: M1 Max 64 GB as the agent Mac, M5 Pro 24 GB running Nexo.

```
 M1 Max (models.remote: m1-max-64)         M5 Pro (models.local: m5-pro-24)
 ┌───────────────────────────┐   SSH       ┌──────────────────────────────────────────┐
 │ Ollama 127.0.0.1:11434    │◄─tunnel─────┤ localhost:11435 ─► agent, extraction    │
 │  agent + extraction model │             │ Ollama 127.0.0.1:11434                   │
 └───────────────────────────┘             │  router, embeddings, judge               │
                                           │ Neo4j, telemetry, Nexo (Podman or native)│
                                           └──────────────────────────────────────────┘
```

1. M1 Max: `deploy/local/ollama-setup.sh m1-max-64 heavy`; enable Remote Login.
2. M5 Pro: `make env PRESET=mac-two-machines` (profiles `m5-pro-24` and `m1-max-64`, tunnel port
   11435), then `ssh -N -L 11435:127.0.0.1:11434 you@m1-max.local` and keep it running.
3. M5 Pro: `make up` (serves the `light` set here), then `make check`.

Never bind Ollama to `0.0.0.0` on a shared network: it has no authentication.

# Verify

`make check` (or `nexo models --check`) confirms each role's server answers and serves its model
([what failures mean](local-setup.md#when-it-does-not-work)); before the first `make up`, models it
has yet to pull show as `todo`, not `FAIL`. `NEXO_LIVE=1 pytest -m live -v` checks the real models: schema-valid JSON from the extraction and router
models, embeddings that rank related text (English and Spanish) above unrelated text, and an agent model
that calls its tools over the OpenAI protocol.[^live] `ollama ps` on each Mac should list the profile's
models at 100% GPU.

# Gotchas

- **Context length.** Below about 23 GiB of GPU memory Ollama defaults to a 4096-token context and
  truncates longer prompts silently; the setup script sets `OLLAMA_CONTEXT_LENGTH` from the profile.
  If Ollama already runs (e.g. the menu-bar app), its settings stay at the defaults and the script
  says so in a banner. `make models APPLY=1` (`ollama-setup.sh --apply`) runs `launchctl setenv` for
  each setting and installs a LaunchAgent that repeats it at login, since `launchctl setenv` does not
  survive a reboot; then quit and reopen the app.[^setup] `make check` and `nexo models --check` warn
  when a loaded model runs with less context than the profile sets.[^check]
- **GPU memory limit.** If a model does not fit, raise macOS's GPU wired-memory limit until reboot with
  `sudo sysctl iogpu.wired_limit_mb=<MB>`, or pick a smaller profile.
- **Speed.** Expect tens of tokens per second; profiles raise `LLM_TIMEOUT_S` (300) and
  `AGENT_TIMEOUT_S` (900) and lower `EXTRACTION_CONCURRENCY` to the tier's parallelism.
- **Changing embedding models** requires re-ingesting everything.[^ollama]
- **MLX builds.** On Apple Silicon some Ollama tags (`-mlx`, `-nvfp4`, and defaults such as
  `qwen3.5:9b`) are MLX builds, which cannot constrain output to a JSON schema (verified: HTTP 501 with
  Ollama 0.40.6 from Homebrew), so extraction and routing fail. The profiles pin GGUF tags (`-q4_K_M`);
  `nexo models --check` catches an MLX model set by hand.

On Linux, or to use a Linux GPU machine as the remote, see [Local on Linux](local-linux.md).
See [ADR-0012](../decisions/adr-0012-local-apple-silicon.md).

[^profiles]: Hardware profile catalog
[^setup]: Ollama setup script
[^env]: Local Nexo environment
[^compose]: nexo setup renderer
[^preset]: mac-single preset
[^native]: mac-native preset
[^check]: nexo setup preflight
[^adr-0020]: "ADR-0020: Native mode in the setup file"
[^ollama]: Ollama
[^live]: Live model checks

# The Mac's own metrics

The stack's collector runs in the Podman VM, so its host metrics describe the VM, not the Mac, and
Ollama runs natively, outside every container. `make mac-metrics` installs a second, native collector
(otelcol-contrib, pinned and checksum-verified) as a user LaunchAgent that starts at login. Every 30
seconds it sends the Mac's memory, swap (`system.paging.*`), CPU, load and disk, and the `ollama`
processes' own CPU and memory (`process.*`), to the stack's collector on `127.0.0.1:4318` (`OTLP_PORT=`
for another port), with the same `host.name` as the stack, so SigNoz shows one host. Its series carry
`nexo.metrics.source=mac-native`.

`make check` reports it once installed (`mac metrics`: running, or how to restart it); its log is
`~/.local/share/nexo/mac-metrics/otelcol.log`. `make mac-metrics-down` stops and removes it. Linux needs
none of this: the stack's collector already sees the real host.
