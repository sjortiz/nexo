---
type: Architecture Decision
title: "ADR-0017: Secrets from a provider, not a file"
description: Generated secrets live in a provider chosen in nexo.yaml - the OS keychain by default, the environment to opt out, SOPS or Vault to opt in - and reach compose through the process environment; one stack per machine shares them.
tags: [adr, operations, setup, secrets]
status: draft
decision_status: accepted
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-20T19:00:00Z }
stale_after: 2027-01-20T00:00:00Z
sources:
  - id: store
    resource: ../../src/nexo/secret_store.py
    title: Secret providers
  - id: envfile
    resource: ../../src/nexo/setup/envfile.py
    title: nexo setup secrets
  - id: adr-0016
    resource: adr-0016-declarative-setup-file.md
    title: "ADR-0016: A declarative setup file"
---

# Context

[ADR-0016](adr-0016-declarative-setup-file.md) kept every secret in each checkout's `.env`. The compose
project is one per machine (`metadata.name`, default `nexo`), so a second checkout with its own
`.env` met volumes made with other passwords: SigNoz failed authentication and Neo4j would have too.
Sharing a plain-text secrets file across machines, or feeding one to `kubectl`, was rejected: secrets
must not travel as plain files between machines.[^adr-0016]

# Decision

- **One stack per machine, secrets from a provider** set in `nexo.yaml` (`spec.secrets.provider`):[^store]

  | Provider | Where | Role |
  |---|---|---|
  | `keychain` | The OS keychain (macOS Keychain, Linux Secret Service), entries `nexo/<stack>`; where none is set up, `~/.config/nexo/config` (`[stack]` sections, owner-only) | Default; opt out with another provider |
  | `env` | The environment (or `.env`); Nexo never writes them | Opt-out: Kubernetes Secrets, External Secrets Operator, CI, `op run`, Vault Agent |
  | `sops` | A SOPS-encrypted file (age, PGP or cloud KMS keys); plain text only in memory | Opt-in |
  | `vault` | HashiCorp Vault or OpenBao, KV version 2 | Opt-in |

- **No secret file is written for compose**: `nexo setup` passes the values through the process
  environment, and the nexo container gets them as environment variables.[^envfile]
- **Nexo reads the secrets it uses** (`NEO4J_PASSWORD`, `VLLM_API_KEY`, `EMBEDDING_API_KEY`) from the
  same provider, after the environment and before `.env`.
- **Migration**: generated secrets found in an older `.env` move into the provider and are blanked there,
  so existing volumes keep working.
- **`check` and `up` log in to Neo4j**: its healthcheck only probes HTTP, so a stale volume looked healthy.

# Options considered

| Option | Against |
|---|---|
| A compose project per checkout | Each checkout's graph separate from the others; the user chose one stack per machine. |
| One secrets file per machine, copied where needed | Plain-text secrets travelling between machines and into clusters. |
| Detect the mismatch only | The second checkout stays broken until someone copies values by hand. |

# Consequences

Several machines or a cluster never receive a copied file: each machine's keychain, or the
cluster's own secret manager through `env`, `sops` or `vault`, holds the values. The SigNoz and
Langfuse logins are read from the keychain (`uv run keyring get nexo/nexo SIGNOZ_USER_PASSWORD`).
Plain `podman compose up` needs the values in its environment; `nexo setup compose` supplies them.

[^store]: Secret providers
[^envfile]: nexo setup secrets
[^adr-0016]: "ADR-0016: A declarative setup file"
