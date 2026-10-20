---
type: Playbook
title: Local on Linux
description: Run Nexo on Linux with Ollama on a GPU (gpu-<VRAM> profiles) or CPU, services in Podman or native, alone or as the model machine for a Mac.
resource: ../../examples/setups/linux-gpu/nexo.yaml
tags: [operations, local, linux, ollama, podman, gpu]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
stale_after: 2027-01-19T00:00:00Z
sources:
  - id: render
    resource: ../../src/nexo/setup/render.py
    title: nexo setup renderer (host network on Linux)
  - id: presets
    resource: ../../examples/setups/linux-gpu/nexo.yaml
    title: Linux GPU preset
  - id: profiles
    resource: ../../src/nexo/local_profiles.py
    title: Hardware profile catalog
  - id: setup
    resource: ../../deploy/local/ollama-setup.sh
    title: Ollama setup script
  - id: ollama-linux
    resource: https://docs.ollama.com/linux
    title: Ollama on Linux
  - id: neo4j-debian
    resource: https://neo4j.com/docs/operations-manual/current/installation/linux/debian/
    title: Neo4j on Debian and Ubuntu
  - id: nvidia-cdi
    resource: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/cdi-support.html
    title: NVIDIA Container Toolkit, CDI support
---

# What is supported

| Area | Status on Linux |
|---|---|
| Nexo code and test suite | Verified: the full suite passes in `python:3.12-slim` (Linux, aarch64) with Neo4j 5.26. |
| Models with Ollama | Same catalog as macOS, with `gpu-<VRAM GB>` or `cpu-<RAM GB>` profiles. |
| Podman path | Supported: `nexo setup` puts the Nexo container on the host network. |
| Native path | Neo4j from Neo4j's Debian repository; telemetry off, or the collector and SigNoz containers. |
| Production vLLM on NVIDIA (`linux-vllm` preset) | Linux is the target platform; GPU passthrough is not tested by this project. |

How the pieces fit together, and how to check them with `nexo models --check`:
[How local setup works](local-setup.md).

# Profiles

| Machine | Profile | Notes |
|---|---|---|
| NVIDIA/AMD GPU | `gpu-<VRAM GB>`, e.g. `gpu-24` (RTX 4090), `gpu-48`, `gpu-80` | Tier from VRAM; models above 64 GB use the 64 GB tier. |
| CPU only | `cpu-<RAM GB>`, e.g. `cpu-64` | Works, at a few tokens per second; MoE models (`qwen3:30b-a3b-...`) are the least slow. |

Tiers and models are the same as on macOS: see the profile table in
[Local on Apple Silicon](local-apple-silicon.md#hardware-profiles).[^profiles]

# Models

1. Install Ollama: `curl -fsSL https://ollama.com/install.sh | sh`. It installs a systemd service and
   uses an NVIDIA (CUDA) or AMD (ROCm) GPU when present.[^ollama-linux]
2. `deploy/local/ollama-setup.sh gpu-24` (or `cpu-64`). With the service already running, the script
   pulls and preloads the profile's models through it and prints the settings to apply.[^setup]
3. Apply them once: `sudo systemctl edit ollama`, add under `[Service]` one
   `Environment="OLLAMA_CONTEXT_LENGTH=..."` line per printed setting, then
   `sudo systemctl restart ollama`. Keep `OLLAMA_HOST` on `127.0.0.1`.

# Podman path

Rootless Podman on Linux cannot reach host services bound to `127.0.0.1` through
`host.containers.internal` (verified with Podman 6.1 on Fedora; it works on macOS), so the Nexo
container uses the host network; `nexo setup` renders that on Linux:[^render]

```bash
make env PRESET=linux-gpu      # nexo.yaml (set models.local.profile: gpu-24); secrets in the keychain
make up                        # models, Neo4j, the collector, SigNoz
make app ARGS="ingest /corpus/memo.md"
```

Neo4j, the collector and SigNoz (UI on http://localhost:8080) keep their own networks and published
ports; only the Nexo container shares the host network. `make up LANGFUSE=1` or `JAEGER=1` adds
Langfuse or Jaeger for one run; `addons:` in `nexo.yaml` keeps them on.[^presets]

# Native path

1. Neo4j from Neo4j's Debian repository (Ubuntu/Debian; RPM and tarball installs also work), set the
   initial password with `neo4j-admin dbms set-initial-password`, then `sudo systemctl start neo4j`.[^neo4j-debian]
2. Telemetry (optional): leave `OTEL_EXPORTER_OTLP_ENDPOINT` empty, or run just the collector and
   SigNoz containers: `make env && make render`, then
   `podman compose -f .nexo/render/compose.yaml up -d otel-collector signoz`.
3. Install uv (`curl -LsSf https://astral.sh/uv/install.sh | sh`), then `uv sync && source .venv/bin/activate`,
   `cp deploy/local/local.env.example .env`, then `nexo ...`. uv installs Python 3.12 itself.

# Linux as the model machine for a Mac

A Linux GPU box can serve the agent and extraction models while a Mac runs Nexo:

1. Linux: `deploy/local/ollama-setup.sh gpu-48 heavy` (and apply the printed settings).
2. Mac: `make env PRESET=mac-two-machines`, set `models.remote.profile: gpu-48` in `nexo.yaml`; then
   `ssh -N -L 11435:127.0.0.1:11434 you@gpu-box` and `make up`.
3. Mac: `make check` should show agent and extraction on `localhost:11435`, all `ok`.

# Production GPU path (vLLM)

`make env PRESET=linux-vllm && make up` (set `HF_TOKEN` in `.env`) requests NVIDIA GPUs through
Compose `deploy.resources`, which
needs Docker with the NVIDIA Container Toolkit. With Podman, generate a CDI spec
(`sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml`) and pass `--device nvidia.com/gpu=all`.[^nvidia-cdi]
Neither is exercised by this project's tests: verify on the target host with `NEXO_LIVE=1 pytest -m live`.

[^render]: nexo setup renderer (host network on Linux)
[^presets]: Linux GPU preset
[^profiles]: Hardware profile catalog
[^setup]: Ollama setup script
[^ollama-linux]: Ollama on Linux
[^neo4j-debian]: Neo4j on Debian and Ubuntu
[^nvidia-cdi]: NVIDIA Container Toolkit, CDI support
