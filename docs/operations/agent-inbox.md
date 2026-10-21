---
type: Playbook
title: Review in Agent Inbox
description: Turn on LangChain's Agent Inbox as an opt-in page for reviewing paused questions.
resource: ../../src/nexo/review_inbox.py
tags: [operations, human-in-the-loop, agent-inbox]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-20T23:00:00Z }
sources:
  - id: adapter
    resource: ../../src/nexo/review_inbox.py
    title: The adapter
  - id: upstream
    resource: https://github.com/langchain-ai/agent-inbox
    title: langchain-ai/agent-inbox
---

# Trigger

You want to review paused questions in a page instead of over MCP or `nexo review`
([ADR-0019](../decisions/adr-0019-review-surfaces.md)). This surface is opt-in.

# Steps

1. Enable it in `nexo.yaml` (or add `AGENT_INBOX=1` to one `make up`):

   ```yaml
   spec:
     addons:
       agentInbox:
         enabled: true
     ports: { agentInbox: 3030, reviewInbox: 2024 }   # optional; these are the defaults
   ```

2. `make up` builds and starts the page at `http://127.0.0.1:3030`. The first build needs network access
   (git, npm and Google Fonts), about 3 GB of free disk in the Podman machine while it runs (the
   finished image is 1.4 GB) and memory for a Next.js build: the Dockerfile caps Node's heap at
   1.5 GB, so a 4 GiB machine running the telemetry stack is enough; at 3 GiB the build is killed.
3. `make review-inbox` serves the adapter on `127.0.0.1:2024`. It reviews as `NEXO_USER` (else your login
   name) and reads passages at `MCP_CLEARANCE` (else `DEFAULT_CLEARANCE`).
4. In the page, add an inbox: graph `nexo`, deployment URL `http://127.0.0.1:2024`, no API key. Open the
   page at `127.0.0.1`, not `localhost`: the adapter answers only that origin.

# Rules

- Licence and upkeep: Agent Inbox is MIT licensed, but the upstream repository is archived. The add-on builds
  commit `f1616f3` from source, publishes no image of its own, and stays opt-in for that reason.[^upstream]
- Nexo runs no LangGraph Server. The adapter serves only the routes the page calls and sends every
  decision to `Runtime.review`.[^adapter]
- Accept approves. Respond rejects with your text as the comment. Ignore rejects with the comment "ignored
  in Agent Inbox". A raw state update (`POST /threads/{id}/state`) is refused, so nothing ends a thread unreviewed.
- A draft with blocking findings cannot be accepted. Use the edit form: set `acknowledge_findings` to `yes`.
  The answer itself cannot be edited, so a changed answer is refused.
- Withheld passages show as "withheld at your clearance", never empty.
- Both services bind `127.0.0.1`. The adapter has no login: anyone on the machine can reach it.

[^upstream]: langchain-ai/agent-inbox
[^adapter]: The adapter
