#!/usr/bin/env bash
# Pull and preload the models one Mac serves, and start Ollama sized for it.
#
#   deploy/local/ollama-setup.sh [--apply] [PROFILE] [single|heavy|light]
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
# unit, macOS app), the script pulls through it and says loudly that its settings
# (context length, keep-alive, KV cache) were not applied. --apply applies them on
# macOS: launchctl setenv now, plus a LaunchAgent that repeats it at every login
# (launchctl setenv alone does not survive a reboot); then quit and reopen Ollama.
# On Linux it prints the systemd drop-in to install. Without a terminal (make up in
# a log), pulls print a plain progress line every 10 s instead of progress bars.
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

apply=0
positional=()
for arg in "$@"; do
  case "$arg" in
    --apply) apply=1 ;;
    *) positional+=("$arg") ;;
  esac
done
set -- "${positional[@]+"${positional[@]}"}"

profile="${1:-$(env_value LOCAL_PROFILE)}"
default_roles=single
[[ -n "$(env_value REMOTE_PROFILE)" ]] && default_roles=light
roles="${2:-$default_roles}"
if [[ -z "$profile" ]]; then
  echo "usage: $0 [--apply] PROFILE [single|heavy|light]   (or set LOCAL_PROFILE), e.g. $0 m1-max-64" >&2
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

apply_macos() {  # launchctl setenv now, and a LaunchAgent that repeats it at login
  local agent="$HOME/Library/LaunchAgents/dev.nexo.ollama-env.plist" setting script=""
  for setting in "${settings[@]}"; do
    launchctl setenv "${setting%%=*}" "${setting#*=}"
    script+="launchctl setenv ${setting%%=*} ${setting#*=}; "
  done
  mkdir -p "$(dirname "$agent")"
  cat >"$agent" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.nexo.ollama-env</string>
  <key>ProgramArguments</key>
  <array><string>/bin/sh</string><string>-c</string><string>${script}</string></array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
PLIST
  echo "applied with launchctl setenv; $agent repeats it at every login (delete it to undo)"
  echo ">>> quit Ollama (menu bar > Quit Ollama) and reopen it: the running server still has its old settings"
}

linux_dropin() {  # the systemd drop-in, printed: installing it needs root
  echo "install this drop-in, then restart Ollama:"
  echo "  sudo mkdir -p /etc/systemd/system/ollama.service.d"
  echo "  sudo tee /etc/systemd/system/ollama.service.d/nexo.conf <<'EOF'"
  echo "[Service]"
  printf 'Environment="%s"\n' "${settings[@]}"
  echo "EOF"
  echo "  sudo systemctl daemon-reload && sudo systemctl restart ollama"
}

if curl -fsS "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1; then
  # A service (Linux systemd unit, macOS app) owns the server: its environment is not
  # ours to set. Pull and preload through it; apply the settings only when asked.
  if [[ "$apply" == 1 && "$(uname -s)" == Darwin ]]; then
    apply_macos
  else
    {
      echo "======================================================================"
      echo "WARNING: ollama is already running at $OLLAMA_HOST, so these settings"
      echo "were NOT applied; it keeps its defaults (4096-token context below"
      echo "~23 GiB of GPU memory: longer prompts are truncated silently):"
      printf '  %s\n' "${settings[@]}"
      if [[ "$(uname -s)" == Darwin ]]; then
        echo "macOS app: re-run with --apply (make models APPLY=1), then quit and reopen Ollama."
      else
        linux_dropin
      fi
      echo "======================================================================"
    } >&2
  fi
else
  log="${TMPDIR:-/tmp}/ollama-nexo.log"
  nohup ollama serve >"$log" 2>&1 &
  echo "started ollama serve (pid $!, log $log)"
  for _ in $(seq 1 30); do curl -fsS "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1 && break; sleep 1; done
fi

pull() {  # progress bars on a terminal; a plain line every 10 s in a log
  if [[ -t 1 ]]; then
    ollama pull "$1"
    return
  fi
  curl -fsSN "http://$OLLAMA_HOST/api/pull" -d "{\"model\":\"$1\"}" | python3 -c '
import json, sys, time
model, last, status = sys.argv[1], float("-inf"), "pulling"
print(f"pull {model}: started", flush=True)
for line in filter(str.strip, sys.stdin):
    event = json.loads(line)
    if error := event.get("error"):
        sys.exit(f"pull {model}: {error}")
    status = event.get("status") or status
    total, done = event.get("total"), event.get("completed")
    if total and done is not None and time.monotonic() - last >= 10:
        last = time.monotonic()
        print(f"pull {model}: {status[:19]} {done / 1e9:.1f} of {total / 1e9:.1f} GB"
              f" ({done * 100 // total}%)", flush=True)
print(f"pull {model}: {status}", flush=True)
' "$1"
}

for model in $MODELS; do
  pull "$model"
done
for model in $MODELS; do  # preload so `ollama ps` shows real memory use
  if [[ " $EMBEDDING_MODELS " == *" $model "* ]]; then
    curl -fsS "http://$OLLAMA_HOST/api/embed" -d "{\"model\":\"$model\",\"input\":\"warm up\"}" >/dev/null
  else
    curl -fsS "http://$OLLAMA_HOST/api/generate" -d "{\"model\":\"$model\",\"prompt\":\"\",\"keep_alive\":-1}" >/dev/null
  fi
done
ollama ps
