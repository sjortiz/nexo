---
type: Reference
title: Open Knowledge Format (OKF)
description: The format this bundle is written in, and the conventions it uses.
resource: https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md
tags: [reference, documentation, okf]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: spec
    resource: https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md
    title: Open Knowledge Format specification, v0.2
---

# Format

OKF v0.2 is a directory of Markdown files with YAML frontmatter. Every concept is one file with a
required `type`; `index.md` and `log.md` are reserved for listings and history; concepts link with
ordinary Markdown links; per-claim citations are footnotes keyed to `sources[].id`.[^spec]

# Conventions in this bundle

This bundle keeps no `log.md`: the history of each change is `git log --oneline` and the pull
request that made it, so nothing is written twice and no two changes contend for one file.

| Convention | Value |
|---|---|
| Root | `docs/`; `index.md` declares `okf_version: "0.2"`. |
| Links | Relative paths, so they also resolve when browsing the repository on GitHub. |
| Code references | `resource` and `sources[].resource` point at files in this repository by relative path. |
| Authorship | `generated.by` uses the actor convention: `claude-code/<model>` for agent-written, `human:<id>` for people. |
| Lifecycle | New or regenerated concepts are `status: draft` until a person reviews them. |
| Trust | After review, add `verified: { by: human:<id>, at: <UTC time> }` and set `status: stable`. |
| Freshness | Concepts tied to fast-moving dependencies (vLLM flags, library behavior) carry `stale_after`. |
| Extension keys | ADRs add `decision_status` (`proposed`, `accepted`, `superseded`). |

`tests/test_docs_okf.py` enforces conformance (frontmatter with `type`, reserved files, resolvable
links, `sources` resources and footnotes) and that every setting is documented in the
[configuration reference](../operations/configuration.md).

[^spec]: Open Knowledge Format specification, v0.2
