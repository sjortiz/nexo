---
okf_version: "0.2"
---

# Nexo knowledge bundle

Knowledge about Nexo, the Graph RAG investigation service, in
[Open Knowledge Format](references/open-knowledge-format.md) v0.2.

# Start here

* [System overview](architecture/overview.md) - What Nexo does and how its three modules fit together.
* [Request lifecycle](architecture/request-lifecycle.md) - One question from intake to an approved answer.
* [LangGraph to Strands handoff](architecture/langgraph-strands-handoff.md) - Where the workflow yields control to the agent.
* [Ways to run Nexo](operations/ways-to-run.md) - Targets, model servers and interfaces, and the command that starts each.
* [Command line](operations/cli.md) - Every `nexo` command and flag.
* [Glossary](architecture/glossary.md) - Nexo's terms, their aliases and where each lives.
* [Observability](operations/observability.md) - Trace shape, metrics, content-capture policy and log correlation.

# Sections

* [Architecture](architecture/) - System shape, data model and control flow.
* [Components](components/) - One concept per pipeline, store, agent, tool and node.
* [Operations](operations/) - Configuration, local stack, review, observability, testing and troubleshooting.
* [Decisions](decisions/) - Architecture decision records and their trade-offs.
* [References](references/) - External guidance the design follows.
