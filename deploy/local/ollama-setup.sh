#!/usr/bin/env bash
# Pull and preload the models one Mac serves, and start Ollama sized for it.
#
#   deploy/local/ollama-setup.sh [PROFILE] [single|heavy|light]
#
# PROFILE is <chip>-<memory GB>, e.g. m1-max-64, m5-pro-24, m1-pro-16; it
# defaults to $LOCAL_PROFILE (from the environment or .env). The role set
# defaults to "single" (this Mac serves everything), or "light" when
# REMOTE_PROFILE is set. On the remote Mac, run: ollama-setup.sh m1-max-64 heavy
#
# The model catalog is src/nexo/local_profiles.py - the same one Nexo reads,
# so the models pulled here are the models Nexo will ask for.
#
# Ollama binds 127.0.0.1 only. The other Mac reaches it through an SSH tunnel,
# and Podman containers through host.containers.internal. Never expose port
# 11434 on the LAN: Ollama has no authentication.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
env_value() {  # value of KEY from the environment, else from .env
  local key="$1"
  if [[ -n "${!key:-}" ]]; then printf '%s' "${!key}"; return 0; fi
  if [[ -f "$repo/.env" ]]; then sed -n "s/^${key}=//p" "$repo/.env" | tail -1; fi
  return 0
}

profile="${1:-$(env_value LOCAL_PROFILE)}"
default_roles=single
[[ -n "$(env_value REMOTE_PROFILE)" ]] && default_roles=light
roles="${2:-$default_roles}"
if [[ -z "$profile" ]]; then
  echo "usage: $0 PROFILE [single|heavy|light]   (or set LOCAL_PROFILE), e.g. $0 m1-max-64" >&2
  exit 2
fi

command -v python3 >/dev/null || { echo "python3 is required (xcode-select --install)" >&2; exit 1; }
plan="$(python3 "$repo/src/nexo/local_profiles.py" plan "$profile" "$roles" --format shell)"
eval "$plan"
echo "profile $profile, role set $roles: ${MODELS} (~${WEIGHTS_GB} GB of weights, context ${CONTEXT_LENGTH})"

command -v ollama >/dev/null || { echo "install Ollama first: brew install ollama" >&2; exit 1; }

# Explicit context: below ~23 GiB of GPU memory Ollama defaults to 4096 tokens
# and silently truncates longer prompts; the OpenAI API cannot change it.
export OLLAMA_CONTEXT_LENGTH="$CONTEXT_LENGTH"
export OLLAMA_NUM_PARALLEL="$NUM_PARALLEL"
export OLLAMA_MAX_LOADED_MODELS="$MAX_LOADED_MODELS"
export OLLAMA_KEEP_ALIVE=-1            # keep every model resident; no reload latency
export OLLAMA_KV_CACHE_TYPE=q8_0       # halves KV-cache memory vs f16
export OLLAMA_FLASH_ATTENTION=1
export OLLAMA_HOST=127.0.0.1:11434

if curl -fsS "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1; then
  echo "ollama is already running: stop it (or quit the app) so these settings apply" >&2
  exit 1
fi
log="${TMPDIR:-/tmp}/ollama-nexo.log"
nohup ollama serve >"$log" 2>&1 &
echo "started ollama serve (pid $!, log $log)"
for _ in $(seq 1 30); do curl -fsS "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1 && break; sleep 1; done

for model in $MODELS; do
  ollama pull "$model"
done
for model in $MODELS; do  # preload so `ollama ps` shows real memory use
  if [[ " $EMBEDDING_MODELS " == *" $model "* ]]; then
    curl -fsS "http://$OLLAMA_HOST/api/embed" -d "{\"model\":\"$model\",\"input\":\"warm up\"}" >/dev/null
  else
    curl -fsS "http://$OLLAMA_HOST/api/generate" -d "{\"model\":\"$model\",\"prompt\":\"\",\"keep_alive\":-1}" >/dev/null
  fi
done
ollama ps
