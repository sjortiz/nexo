---
type: Architecture Decision
title: "ADR-0017: Secrets from a provider, not a file you copy"
description: Generated secrets live in a provider chosen in nexo.yaml - a private per-user file by default, the OS keychain, SOPS or Vault as opt-ins; a cluster mounts them as files and Nexo reads them in-process, never as environment variables.
tags: [adr, operations, setup, secrets, kubernetes]
status: draft
decision_status: accepted
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-20T20:00:00Z }
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
project is one per machine (`metadata.name`, default `nexo`), so a second checkout with its own `.env`
met volumes made with other passwords: SigNoz failed authentication, and Neo4j would have too.[^adr-0016]
Two constraints from the user: secrets must not travel between machines as plain files, and an
enterprise Kubernetes setup must not load them as environment variables, which leak through pod
specs and `kubectl describe`, `/proc/<pid>/environ`, child processes and crash dumps.

# Decision

- **One stack per machine; secrets from a provider** set in `nexo.yaml` (`spec.secrets.provider`):[^store]

  | Provider | Where | Role |
  |---|---|---|
  | `file` | `~/.config/nexo/config`: one `[stack]` section, 0600 in a 0700 directory, written by `nexo setup` | Default on a single machine |
  | `file` + `path` | A file or a directory of per-key files mounted by the platform (Kubernetes Secret volume, Secrets Store CSI driver, a file CI writes); read-only, any permissions | Clusters and CI |
  | `keychain` | The OS keychain (macOS Keychain, Linux Secret Service), entries `nexo/<stack>` | Opt-in |
  | `sops` | A SOPS-encrypted file (age, PGP or cloud KMS keys); plain text only in memory | Opt-in |
  | `vault` | HashiCorp Vault or OpenBao, KV version 2 | Opt-in |

- **Nexo reads the secrets it uses in-process** (`NEO4J_PASSWORD`, `VLLM_API_KEY`, `EMBEDDING_API_KEY`):
  a settings source asks the provider; nothing is copied into `os.environ`.
- **Kubernetes rule** for the future cluster target: secrets are mounted as files (a Secret volume, or
  the CSI driver with a `SecretProviderClass` backed by Vault or a cloud secret manager). Manifests never
  use `env.valueFrom.secretKeyRef`, `envFrom` or CSI `syncSecret` into environment variables for secrets.
- **Local only**: on your own machine `nexo setup` hands the values to compose through the process
  environment, and the local nexo container receives them as environment variables; no secrets file is
  written for compose.[^envfile]
- **Migration**: generated secrets found in an older `.env` move into the provider and are blanked there,
  so existing volumes keep working.
- **`check` and `up` log in to Neo4j**: its healthcheck only probes HTTP, so a stale volume looked healthy.

# Options considered

| Option | Against |
|---|---|
| A compose project per checkout | Each checkout's graph separate from the others; the user chose one stack per machine. |
| One secrets file per machine, copied where needed | Plain-text secrets travelling between machines and into clusters. |
| Environment variables in clusters (`secretKeyRef`, `envFrom`) | They leak through pod specs, `/proc`, child processes and crash dumps. |
| The keychain as the default | Fine on a desktop; the user preferred a plain per-user file by default, the keychain opt-in. |
| Detect the mismatch only | The second checkout stays broken until someone copies values by hand. |

# Consequences

No machine or cluster receives a copied file: each machine writes its own `~/.config/nexo/config`, and
a cluster's secret manager mounts what its pods read. The SigNoz and Langfuse logins are in
`~/.config/nexo/config` under `[nexo]`. Plain `podman compose up` needs the values in its environment;
`nexo setup compose` supplies them.

[^store]: Secret providers
[^envfile]: nexo setup secrets
[^adr-0016]: "ADR-0016: A declarative setup file"
