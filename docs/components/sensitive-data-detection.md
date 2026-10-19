---
type: Component
title: Sensitive data detection
description: PII/PHI classification at ingestion, redaction policy, and caller clearance at query time.
resource: ../../src/nexo/storage/sensitive_data.py
tags: [module-a, security, pii, phi, compliance]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: sensitive
    resource: ../../src/nexo/storage/sensitive_data.py
    title: Detectors
  - id: comprehend
    resource: https://docs.aws.amazon.com/comprehend-medical/latest/dev/textanalysis-phi.html
    title: Amazon Comprehend Medical, detect PHI
  - id: aws-health
    resource: ../references/aws-rag-healthcare.md
    title: AWS guidance, RAG for healthcare use cases
---

# Sensitivity levels

Ordered `public` < `internal` < `pii` < `phi`. A chunk's level is the higher of the document's declared
level and anything detected. Entities and relationships take the level of their **least** sensitive
supporting chunk (`sensitivity_rank`): a caller sees them if any source they are cleared for supports
them, while a fact stated only in a PHI document stays PHI. Entities carry matching `:PII` / `:PHI`
labels; both are recomputed whenever a document is added, replaced or withdrawn.

# Providers (`SENSITIVE_DATA_PROVIDER`)

| Provider | Detects | Notes |
|---|---|---|
| `none` | nothing | Declared level only. |
| `regex` (default) | email, US SSN, Dominican cédula, phone, Luhn-valid payment card (PII); MRN (PHI) | High precision, low recall. A baseline, not a PHI control. |
| `comprehend_medical` | PHI entities from `DetectPHI` | Sends chunk text to an AWS regional endpoint; enable only where approved (BAA, region). Needs `AWS_REGION` and the `aws` extra.[^comprehend] |

# Policy (`SENSITIVE_DATA_POLICY`)

- `tag` (default): keep the text, raise the chunk's level.
- `redact`: replace detected spans with `[CATEGORY]` before embedding or extraction.

# Clearance at query time

`caller_clearance` (default `DEFAULT_CLEARANCE=internal`) is set by the authenticated service layer and
copied into the router's `ChunkFilter.max_sensitivity`; the router LLM cannot change it. It is enforced
in [fetch_vector_chunks](fetch-vector-chunks.md) (withheld chunks never reach the model),
[query_knowledge_graph](query-knowledge-graph.md) (redacts node maps above clearance) and, on
Enterprise, by database roles. [Compliance](compliance-checks.md) blocks approval of answers that rely
on PHI or contain detected identifiers until the reviewer acknowledges it. This follows the
healthcare guidance's emphasis on fine-grained access control and human review.[^aws-health]

See [ADR-0006](../decisions/adr-0006-sensitivity-and-caller-clearance.md).[^sensitive]

[^sensitive]: Detectors
[^comprehend]: Amazon Comprehend Medical, detect PHI
[^aws-health]: AWS guidance, RAG for healthcare use cases
