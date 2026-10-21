# Run

* [Choosing a setup](decision-guide.md) - Decision matrices: preset per hardware, one or two machines, model serving, observability add-ons, memory per combination.
* [Configuration reference](configuration.md) - Every environment variable, with defaults and effects.
* [Compose stack with vLLM](local-stack.md) - Bring up Neo4j, vLLM on GPUs, tracing and the CLI with Docker Compose.
* [How local setup works](local-setup.md) - Model roles, the profile catalog, who listens where, and `nexo models --check`.
* [Swapping and adding models](models.md) - Change one role's model, add a judge, point a role at another machine, confirm it.
* [Local on Apple Silicon](local-apple-silicon.md) - Ollama on one or two Macs, model roles split across machines.
* [Local on Linux](local-linux.md) - Ollama on a GPU or CPU, Podman with host networking or native; Linux as the model machine for a Mac.
* [Use from Claude Code](claude-code.md) - The nexo MCP server and the nexo-knowledge skill.
* [Human review](human-review.md) - How a reviewer reads the packet and approves or rejects.
* [Review in Agent Inbox](agent-inbox.md) - The opt-in Agent Inbox page, its adapter and licence note.
* [Deleting a source](delete.md) - `nexo delete` for operators on the allowlist, and its audit record.

# Observe and verify

* [Observability](observability.md) - Span catalog and what each trace shows.
* [Testing](testing.md) - Offline suite, Neo4j integration tests and mutation checks.
* [Evaluation playbook](evaluation.md) - Run the golden set and read the metrics.

# Fix

* [Troubleshooting](troubleshooting.md) - Symptoms, causes and fixes for common failures.
* [FAQ](faq.md) - Which trace backend to use, what Nexo is and is not, data and model questions.
