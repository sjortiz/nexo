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
# Works on macOS and Linux. If Ollama already runs as a service (Linux systemd
# unit, macOS app), the script pulls through it and prints the settings to apply.
# Ollama binds 127.0.0.1 only. The other machine reaches it through an SSH
# tunnel; containers through host.containers.internal (macOS) or the host
# network (Linux). Never expose port 11434 on the LAN: Ollama has no authentication.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
env_file="${NEXO_ENV_FILE:-$repo/.env}"
env_value() {  # value of KEY from the environment, else from the env file
  local key="$1"
  if [[ -n "${!key:-}" ]]; then printf '%s' "${!key}"; return 0; fi
  if [[ -f "$env_file" ]]; then sed -n "s/^${key}=//p" "$env_file" | tail -1; fi
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

command -v python3 >/dev/null || { echo "python3 is required (macOS: xcode-select --install; Linux: your package manager)" >&2; exit 1; }
plan="$(python3 "$repo/src/nexo/local_profiles.py" plan "$profile" "$roles" --format shell)"
eval "$plan"
echo "profile $profile, role set $roles: ${MODELS} (~${WEIGHTS_GB} GB of weights, context ${CONTEXT_LENGTH})"

command -v ollama >/dev/null || {
  echo "install Ollama first - macOS: brew install ollama; Linux: curl -fsSL https://ollama.com/install.sh | sh" >&2
  exit 1
}

# Explicit context: below ~23 GiB of GPU memory Ollama defaults to 4096 tokens
# and silently truncates longer prompts; the OpenAI API cannot change it.
settings=(
  "OLLAMA_CONTEXT_LENGTH=$CONTEXT_LENGTH"
  "OLLAMA_NUM_PARALLEL=$NUM_PARALLEL"
  "OLLAMA_MAX_LOADED_MODELS=$MAX_LOADED_MODELS"
  "OLLAMA_KEEP_ALIVE=-1"        # keep every model resident; no reload latency
  "OLLAMA_KV_CACHE_TYPE=q8_0"   # halves KV-cache memory vs f16
  "OLLAMA_FLASH_ATTENTION=1"
)
export "${settings[@]}"
export OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"
case "${OLLAMA_HOST%:*}" in
  127.0.0.1|localhost|"") ;;
  *) echo "warning: OLLAMA_HOST=$OLLAMA_HOST is not loopback; Ollama has no authentication" >&2 ;;
esac

if curl -fsS "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1; then
  # A service (Linux systemd unit, macOS app) owns the server: we cannot set its
  # environment from here. Pull and preload through it, and say what to apply.
  echo "ollama is already running at $OLLAMA_HOST; apply these settings to it and restart it:" >&2
  printf '  %s\n' "${settings[@]}" >&2
  echo "  Linux (systemd): sudo systemctl edit ollama  ->  [Service] Environment=\"KEY=VALUE\" per line" >&2
  echo "  macOS app: launchctl setenv KEY VALUE per line, then quit and reopen Ollama" >&2
else
  log="${TMPDIR:-/tmp}/ollama-nexo.log"
  nohup ollama serve >"$log" 2>&1 &
  echo "started ollama serve (pid $!, log $log)"
  for _ in $(seq 1 30); do curl -fsS "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1 && break; sleep 1; done
fi

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
