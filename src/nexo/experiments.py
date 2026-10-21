"""`nexo eval` as a Langfuse experiment, when the langfuse add-on is on (LANGFUSE_HOST set).

Langfuse v4 builds experiments from OpenTelemetry attributes:

* The golden set is upserted over the public API as a dataset (one item per case, keyed
  by a hash of the dataset name and the case's input, so a rerun updates the same items).
* Each `eval.case` span starts its own trace and is the experiment item's root: the
  experiment's ids travel in baggage (telemetry.BaggageAttributes copies them onto every
  span of the case), the item's id and version are set on the root.
* Each case's metrics are posted as numeric scores on its root span.

Only the golden case goes to Langfuse as content (question, reference answer, expected
chunks and phrases, from the file you wrote). Answers stay off spans, as everywhere
else: the collector strips content.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from opentelemetry import baggage, context, trace
from opentelemetry.context import Context
from opentelemetry.trace import Span

from nexo.config import Settings
from nexo.telemetry import traced_span

log = logging.getLogger(__name__)

_EXPERIMENT = "langfuse.experiment"


class LangfuseError(RuntimeError):
    """Langfuse refused or could not be reached; the message says which call (never a key)."""


class LangfuseClient:
    """The few Langfuse public API calls `nexo eval` makes, with Basic auth."""

    def __init__(
        self, host: str, public_key: str, secret_key: str, timeout: float = 10
    ) -> None:
        self.base = host.rstrip("/") + "/api/public"
        token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self._auth = f"Basic {token}"
        self.timeout = timeout

    def _post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode(),
            headers={"Authorization": self._auth, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise LangfuseError(f"langfuse {path}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise LangfuseError(
                f"langfuse {path}: {self.base} unreachable "
                "(is the add-on up? make up LANGFUSE=1)"
            ) from exc

    def upsert_dataset(self, name: str, description: str) -> str:
        """Create the dataset or update its description; return its id."""
        return self._post("/v2/datasets", {"name": name, "description": description})[
            "id"
        ]

    def upsert_item(self, dataset: str, key: str, **fields: Any) -> str:
        """Create or update one dataset item; return its version (a timestamp)."""
        body = {"datasetName": dataset, "id": key, **fields}
        return self._post("/dataset-items", body)["version"]

    def score(
        self, score_id: str, trace_id: str, observation_id: str, name: str, value: float
    ) -> None:
        """A numeric score on one observation; the same id overwrites it."""
        self._post(
            "/scores",
            {
                "id": score_id,
                "traceId": trace_id,
                "observationId": observation_id,
                "name": name,
                "value": value,
                "dataType": "NUMERIC",
            },
        )


def case_input(case: Mapping[str, Any], settings: Settings) -> dict[str, str]:
    """What the investigator is given for a case."""
    return {
        "question": case["question"],
        "strategy": case.get("strategy", "hybrid"),
        "clearance": case.get("clearance", settings.default_clearance),
    }


def item_id(dataset: str, inputs: Mapping[str, str]) -> str:
    """A case's dataset item id: stable across runs, unique across datasets."""
    keyed = json.dumps([dataset, inputs], sort_keys=True).encode()
    return hashlib.sha256(keyed).hexdigest()[:32]


def _expected_output(case: Mapping[str, Any]) -> dict[str, Any]:
    """What a case is scored against."""
    expected = {
        "answer": case.get("reference_answer"),
        "relevant_chunk_ids": case.get("relevant_chunk_ids"),
        "answer_contains": case.get("answer_contains"),
    }
    return {k: v for k, v in expected.items() if v is not None}


@dataclass
class Experiment:
    """One `nexo eval` run in Langfuse: the dataset, the run's ids and each case's item."""

    client: LangfuseClient
    settings: Settings
    dataset: str
    run_id: str
    run_name: str
    # Every span of the run carries these: the run's ids, its dataset's and its metadata.
    run_baggage: dict[str, str] = field(default_factory=dict)
    # item id -> item version
    versions: dict[str, str] = field(default_factory=dict)

    @classmethod
    def start(
        cls,
        settings: Settings,
        cases: list[dict[str, Any]],
        dataset: str,
        run_name: str | None = None,
    ) -> Experiment:
        """Upsert the dataset and its items; raise LangfuseError if Langfuse refuses."""
        if not (settings.langfuse_public_key and settings.langfuse_secret_key):
            raise LangfuseError(
                "LANGFUSE_HOST is set but LANGFUSE_PUBLIC_KEY or LANGFUSE_SECRET_KEY "
                "is not: run make env"
            )
        client = LangfuseClient(
            settings.langfuse_host,
            settings.langfuse_public_key.get_secret_value(),
            settings.langfuse_secret_key.get_secret_value(),
        )
        dataset_id = client.upsert_dataset(dataset, "nexo eval golden set")
        versions = {}
        for case in cases:
            inputs = case_input(case, settings)
            key = item_id(dataset, inputs)
            versions[key] = client.upsert_item(
                dataset, key, input=inputs, expectedOutput=_expected_output(case)
            )
        agent_model = settings.endpoint("agent").model
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_id, run_name = uuid.uuid4().hex, run_name or f"{agent_model} {stamp}"
        run_baggage = {
            f"{_EXPERIMENT}.id": run_id,
            f"{_EXPERIMENT}.name": run_name,
            f"{_EXPERIMENT}.dataset.id": dataset_id,
            f"{_EXPERIMENT}.metadata.agent_model": agent_model,
        }
        if settings.judge_model:
            run_baggage[f"{_EXPERIMENT}.metadata.judge_model"] = settings.judge_model
        return cls(
            client=client,
            settings=settings,
            dataset=dataset,
            run_id=run_id,
            run_name=run_name,
            run_baggage=run_baggage,
            versions=versions,
        )

    def _baggage(self, entries: Mapping[str, str], ctx: Context) -> Context:
        for key, value in entries.items():
            ctx = baggage.set_baggage(key, value, ctx)
        return ctx

    @contextmanager
    def case_span(self, case: Mapping[str, Any]) -> Iterator[Span]:
        """The case's `eval.case` span: a new trace, rooted at this experiment item."""
        # An empty context: no parent span, so each case is its own trace.
        run_context = self._baggage(self.run_baggage, Context())
        with traced_span("eval.case", context=run_context) as span:
            key = item_id(self.dataset, case_input(case, self.settings))
            item = {
                f"{_EXPERIMENT}.item.id": key,
                f"{_EXPERIMENT}.item.version": self.versions[key],
                f"{_EXPERIMENT}.item.root_observation_id": _span_id(span),
            }
            span.set_attributes(item)
            # The span made current by traced_span sits on the caller's context, which has
            # no run baggage: rebuild the children's context from the run's.
            case_context = trace.set_span_in_context(span, run_context)
            token = context.attach(self._baggage(item, case_context))
            try:
                yield span
            finally:
                context.detach(token)

    def score(self, span: Span, metrics: Mapping[str, float | None]) -> None:
        """Post the case's non-null metrics as scores on its root span; log, never raise."""
        trace_id = format(span.get_span_context().trace_id, "032x")
        observation_id = _span_id(span)
        for name, value in metrics.items():
            if value is None:
                continue
            try:
                self.client.score(
                    f"{self.run_id}-{observation_id}-{name}",
                    trace_id,
                    observation_id,
                    name,
                    value,
                )
            except LangfuseError as exc:
                log.warning("score %s not posted: %s", name, exc)


def _span_id(span: Span) -> str:
    return format(span.get_span_context().span_id, "016x")
