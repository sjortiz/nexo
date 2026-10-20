#!/usr/bin/env bash
# Fill missing or empty secrets in an env file with random values; existing values are kept.
#
#   deploy/local/env-secrets.sh [.env]
#
# Covers Neo4j and the self-hosted Langfuse stack (deploy/langfuse/compose.yml).
set -euo pipefail

file="${1:-.env}"
touch "$file"
chmod 600 "$file"

hex() { openssl rand -hex "$1"; }

ensure() {  # KEY VALUE: set KEY only when it is absent or empty
  local key="$1" value="$2" tmp
  if grep -qE "^${key}=.+" "$file"; then
    return 0
  fi
  if grep -qE "^${key}=$" "$file"; then
    tmp="$(mktemp)"
    sed "s|^${key}=\$|${key}=${value}|" "$file" > "$tmp"
    cat "$tmp" > "$file" && rm -f "$tmp"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
  echo "generated $key"
}

ensure NEO4J_PASSWORD "$(hex 16)"
ensure LANGFUSE_PUBLIC_KEY "pk-lf-$(hex 16)"
ensure LANGFUSE_SECRET_KEY "sk-lf-$(hex 24)"
ensure LANGFUSE_USER_PASSWORD "$(hex 12)"
ensure LANGFUSE_SALT "$(hex 16)"
ensure LANGFUSE_ENCRYPTION_KEY "$(hex 32)"  # 64 hex characters, as Langfuse requires
ensure LANGFUSE_NEXTAUTH_SECRET "$(hex 32)"
ensure LANGFUSE_POSTGRES_PASSWORD "$(hex 16)"
ensure LANGFUSE_CLICKHOUSE_PASSWORD "$(hex 16)"
ensure LANGFUSE_REDIS_PASSWORD "$(hex 16)"
ensure LANGFUSE_S3_SECRET "$(hex 16)"
