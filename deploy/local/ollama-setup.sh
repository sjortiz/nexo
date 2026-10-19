#!/usr/bin/env bash
# Pull the models for one Mac and start Ollama with settings sized for it.
#
#   deploy/local/ollama-setup.sh m1-max-32   # agent + extraction model
#   deploy/local/ollama-setup.sh m1-max-64   # agent + extraction model (larger)
#   deploy/local/ollama-setup.sh m5-pro-24   # router, embeddings, judge
#   deploy/local/ollama-setup.sh single-64   # everything on one 64 GB Mac
#
# Ollama binds 127.0.0.1 only; the other Mac reaches it through an SSH tunnel
# (see docs/operations/local-apple-silicon.md). Do not expose port 11434 on the
# LAN: Ollama has no authentication.
set -euo pipefail

profile="${1:-}"
case "$profile" in
  m1-max-32)
    models=(qwen3:30b-a3b-instruct-2507-q4_K_M)
    context=16384 parallel=1 ;;
  m1-max-64)
    models=(qwen3.6:35b-a3b-q4_K_M)
    context=32768 parallel=2 ;;
  m5-pro-24)
    models=(qwen3:4b-instruct-2507 bge-m3 llama3.1:8b)
    context=16384 parallel=1 ;;
  single-64)
    models=(qwen3:30b-a3b-instruct-2507-q4_K_M bge-m3 llama3.1:8b)
    context=32768 parallel=1 ;;
  *)
    echo "usage: $0 {m1-max-32|m1-max-64|m5-pro-24|single-64}" >&2
    exit 2 ;;
esac

command -v ollama >/dev/null || { echo "install Ollama first: brew install ollama" >&2; exit 1; }

# Explicit context: below ~23 GiB of GPU memory Ollama defaults to 4096 tokens
# and silently truncates longer prompts.
export OLLAMA_CONTEXT_LENGTH="$context"
export OLLAMA_NUM_PARALLEL="$parallel"
export OLLAMA_KEEP_ALIVE=-1            # keep every model resident; no reload latency
export OLLAMA_MAX_LOADED_MODELS=3
export OLLAMA_KV_CACHE_TYPE=q8_0       # halves KV-cache memory vs f16
export OLLAMA_FLASH_ATTENTION=1
export OLLAMA_HOST=127.0.0.1:11434

if ! curl -fsS "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1; then
  ollama serve >"${TMPDIR:-/tmp}/ollama-nexo.log" 2>&1 &
  echo "started ollama serve (pid $!, log ${TMPDIR:-/tmp}/ollama-nexo.log)"
  for _ in $(seq 1 30); do curl -fsS "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1 && break; sleep 1; done
else
  echo "ollama already running: restart it so these settings apply" >&2
fi

for model in "${models[@]}"; do
  ollama pull "$model"
done
for model in "${models[@]}"; do  # load now so `ollama ps` shows real memory use
  if [[ "$model" == bge-m3 ]]; then
    curl -fsS "http://$OLLAMA_HOST/api/embed" -d "{\"model\":\"$model\",\"input\":\"warm up\"}" >/dev/null
  else
    curl -fsS "http://$OLLAMA_HOST/api/generate" -d "{\"model\":\"$model\",\"prompt\":\"\",\"keep_alive\":-1}" >/dev/null
  fi
done
ollama ps
