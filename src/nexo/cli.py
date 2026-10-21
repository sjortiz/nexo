"""Command line entry point; `nexo --help` lists the commands."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from datetime import date
from pathlib import Path
from socketserver import BaseServer
from typing import NamedTuple

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, ValidationError

from nexo.agents.investigator import Investigator
from nexo.agents.models import AgentOutput, InvestigationScope
from nexo.agents.redaction import ChunkRedactor
from nexo.config import explain, get_settings, render_drift
from nexo.evaluation import (
    answer_scores,
    extraction_scores,
    grounded_key,
    judge_faithfulness,
    judge_relevancy,
    record_metrics,
    retrieval_scores,
    triple_key,
)
from nexo.experiments import Experiment, LangfuseError
from nexo.llm import (
    build_chat_model,
    build_embeddings,
    check_model_servers,
    resolved_models,
)
from nexo.logging_setup import configure_logging
from nexo.mcp_server import build_mcp_server
from nexo.orchestration.graph import workflow_mermaid
from nexo.orchestration.state import ReviewDecision, describe_errors
from nexo.review_inbox import InboxAdapter, build_server
from nexo.review_ui import ReviewApp, check_loopback, make_server
from nexo.setup.check import context_problem
from nexo.setup.commands import add_parser as add_setup_parser
from nexo.service import (
    RunResult,
    build_investigator,
    chunk_store_from_settings,
    detector_from_settings,
    open_ingestor,
    open_review_runtime,
    open_runtime,
    remove_source,
)
from nexo.storage.graph_store import GraphStore
from nexo.storage.neo4j_chunk_store import Neo4jChunkStore, import_local_store
from nexo.storage.ingest import (
    ExtractionAids,
    IngestionLimits,
    IngestionTargets,
    KnowledgeGraphIngestor,
)
from nexo.storage.ontology import Ontology
from nexo.storage.schemas import DocumentMetadata, Sensitivity, Triple
from nexo.storage.sensitive_data import NoopDetector
from nexo.storage.vector_store import ChunkFilter, ChunkStore, LocalChunkStore
from nexo.telemetry import (
    caller_attributes,
    setup_telemetry,
    shutdown_telemetry,
    traced_span,
)

log = logging.getLogger("nexo.cli")

CLEARANCES = [s.value for s in Sensitivity]


def _emit(
    result, message: str, *args, level: int = logging.INFO, indent: int | None = 2
) -> None:
    """Write `result` to stdout as JSON (the command's output) and log `message` to stderr.

    `indent=None` writes one object per line, for commands that emit rows (JSON Lines).
    The record carries the result too, for OTLP export with content capture.
    """
    if isinstance(result, BaseModel):
        result = result.model_dump(mode="json", exclude_none=True)
    log.log(level, message, *args, extra={"result": result})
    sys.stdout.write(json.dumps(result, indent=indent, default=str) + "\n")
    sys.stdout.flush()


def _ingest(args: argparse.Namespace) -> int:

    metadata = DocumentMetadata(
        source_id=args.source_id or Path(args.file).stem,
        title=args.title or Path(args.file).stem,
        doc_type=args.doc_type,
        source_uri=args.source_uri or Path(args.file).resolve().as_uri(),
        effective_date=(
            date.fromisoformat(args.effective_date) if args.effective_date else None
        ),
        sensitivity=Sensitivity(args.sensitivity),
    )
    with open_ingestor(get_settings()) as ingestor:
        report = ingestor.ingest(Path(args.file).read_text(encoding="utf-8"), metadata)
    if report.failed_chunk_ids:
        _emit(
            report.model_dump(mode="json"),
            "ingested %s with %d failed chunks",
            report.source_id,
            len(report.failed_chunk_ids),
            level=logging.WARNING,
        )
        return 1
    _emit(report, "ingested %s", report.source_id)
    return 0


def _ask(args: argparse.Namespace) -> int:

    with open_runtime(get_settings()) as runtime:
        result = runtime.start(
            args.query,
            thread_id=args.thread_id,
            clearance=args.clearance,
            user=args.user,
        )
        _emit(result, "%s", _summary(result))
    return 0


def _summary(result: RunResult) -> str:
    """A thread's log line: its status and, once completed, how it ended."""
    line = f"thread {result.thread_id}: {result.status}"
    if result.status == "completed" and result.outcome:
        return f"{line} ({result.outcome})"
    return line


def _review(args: argparse.Namespace) -> int:

    try:
        decision = ReviewDecision(
            approval_status="approved" if args.approve else "rejected",
            reviewer=args.reviewer,
            comment=args.comment,
            acknowledged_findings=args.acknowledge_findings,
        )
    except ValidationError as exc:
        log.error("invalid decision: %s", describe_errors(exc))
        return 2
    with open_runtime(get_settings()) as runtime:
        result = runtime.review(args.thread_id, decision)
        _emit(result, "%s", _summary(result))
    return 0


def _review_ui(args: argparse.Namespace) -> int:
    """Serve the demo review page on loopback; refuses any other host."""
    settings = get_settings()
    try:
        check_loopback(args.host)
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    with open_runtime(settings) as runtime:
        server = make_server(ReviewApp(runtime, settings), args.host, args.port)
        log.warning(
            "demo review page (no authentication) at http://%s:%d/",
            *server.server_address[:2],
        )
        _serve(server)
    return 0


def _serve(server: BaseServer) -> None:
    """Serve until Ctrl-C, then close the socket."""
    with server, suppress(KeyboardInterrupt):
        server.serve_forever()


def _delete(args: argparse.Namespace) -> int:
    """Remove a document and what only it supported; operators only (DELETE_OPERATORS)."""

    try:
        record = remove_source(get_settings(), args.source_id)
    except PermissionError as exc:
        log.error("%s", exc)
        return 2
    _emit(
        record,
        "deleted %s: %d chunks removed",
        record["source_id"],
        record["chunks_removed"],
    )
    return 0


def _status(args: argparse.Namespace) -> int:

    with open_runtime(get_settings()) as runtime:
        result = runtime.status(args.thread_id)
        _emit(result, "%s", _summary(result))
    return 0


def _eval(args: argparse.Namespace) -> int:
    """Each JSONL line: {"question", "relevant_chunk_ids": [...], "reference_answer"?,
    "answer_contains"?: [...], "strategy"?: "graph"|"vector"|"hybrid",
    "clearance"?: "public"|"internal"|"pii"|"phi"}.

    Eval skips the router, so `strategy` replays its choice (default hybrid) and `clearance`
    stands for the caller's (default DEFAULT_CLEARANCE). With LANGFUSE_HOST set (the
    langfuse add-on), the run is also a Langfuse experiment on the golden set's dataset.
    """

    settings = get_settings()
    setup_telemetry(settings)
    cases = _read_jsonl(args.dataset)
    experiment = None
    if settings.langfuse_host:
        try:
            experiment = Experiment.start(
                settings,
                cases,
                dataset=args.dataset_name or Path(args.dataset).stem,
                run_name=args.run_name,
            )
        except LangfuseError as exc:
            log.error("%s", exc)
            return 1
        log.info(
            "langfuse experiment %r on dataset %r",
            experiment.run_name,
            experiment.dataset,
        )
    with GraphStore.from_settings(settings, read_only=True) as graph:
        chunks = chunk_store_from_settings(settings, graph)
        investigator = build_investigator(settings, graph, chunks)
        judge = _judge(settings, chunks) if settings.judge_model else None
        failed = 0
        repeats = max(1, getattr(args, "repeat", 1))
        runs: dict[str, list[dict]] = {}  # question -> one metrics dict per repeat
        for rep in range(1, repeats + 1):
            for case in cases:
                with (
                    experiment.case_span(case)
                    if experiment
                    else traced_span("eval.case")
                ) as span:
                    try:
                        scored = _score_case(
                            case, investigator, judge, settings.default_clearance
                        )
                    except Exception as exc:  # pylint: disable=broad-exception-caught
                        # One case must not end the run; the row says why it has no scores.
                        failed += 1
                        row = {
                            "question": case.get("question"),
                            "error": type(exc).__name__,
                        }
                    else:
                        if experiment:
                            experiment.score(span, scored.metrics)
                        row = scored.row
                        runs.setdefault(case["question"], []).append(scored.metrics)
                if repeats > 1:
                    row = {**row, "rep": rep}
                _emit(row, "eval case scored", indent=None)
        if repeats > 1:
            for question, metrics in runs.items():
                _emit(_summary_row(question, metrics), "eval summary", indent=None)
    return 1 if failed else 0


def _summary_row(question: str, runs: list[dict]) -> dict:
    """Mean and spread (max - min) of each metric over the repeats of one question.

    The spread is the noise floor: a difference between two versions smaller than
    their spreads is not a difference.
    """
    keys = sorted({k for m in runs for k, v in m.items() if v is not None})
    mean, spread = {}, {}
    for key in keys:
        values = [m[key] for m in runs if m.get(key) is not None]
        mean[key] = round(sum(values) / len(values), 3)
        spread[key] = round(max(values) - min(values), 3)
    return {"question": question, "reps": len(runs), "mean": mean, "spread": spread}


def _read_jsonl(path: str) -> list[dict]:
    """One JSON object per non-blank line."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class _Judge(NamedTuple):
    """The judge model, and the chunks it reads as the case's caller would see them."""

    model: BaseChatModel
    chunks: ChunkStore
    redactor: ChunkRedactor


def _judge(settings, chunks: ChunkStore) -> _Judge:
    detector = detector_from_settings(settings)
    return _Judge(
        build_chat_model(settings, role="judge"), chunks, ChunkRedactor(detector)
    )


class _Scored(NamedTuple):
    """One golden case's metrics, and its output row (the metrics beside the answer)."""

    metrics: dict[str, float | None]
    row: dict


def _score_case(
    case: dict, investigator: Investigator, judge: _Judge | None, clearance: str
) -> _Scored:
    """Investigate one golden case and score it."""
    scope = InvestigationScope(
        strategy=case.get("strategy", "hybrid"),
        chunk_filter=ChunkFilter(max_sensitivity=case.get("clearance", clearance)),
    )
    if "canned_answer" in case:  # judge-sanity case: score a given answer, run nothing
        output = AgentOutput(answer=case["canned_answer"], stop_reason="canned")
    else:
        output = investigator.investigate(case["question"], scope=scope)
    metrics = retrieval_scores(output, case.get("relevant_chunk_ids", []))
    if "answer_contains" in case:
        metrics.update(answer_scores(output.answer, case["answer_contains"]))
    if judge and output.answer:
        evidence = _judge_evidence(
            judge.chunks, judge.redactor, scope.chunk_filter, output.citations
        )
        metrics["generation.faithfulness"] = judge_faithfulness(
            judge.model, output.answer, evidence
        ).score
        metrics["generation.relevancy"] = judge_relevancy(
            judge.model, case["question"], output.answer
        ).score
    record_metrics(metrics)
    row = {
        "question": case["question"],
        **metrics,
        "answer": output.answer,
        "tool_calls": len(output.tool_calls),
        "stop_reason": output.stop_reason,
    }
    if "canned_answer" in case:
        row["canned"] = True
    return _Scored(metrics, row)


def _judge_evidence(chunks, redactor, chunk_filter, chunk_ids) -> list[str]:
    """Cited chunk texts as the case's caller saw them: masked, or dropped if withheld."""
    shown = (redactor.visible(c, chunk_filter) for c in chunks.get_by_ids(chunk_ids))
    return [c.text for c in shown if c is not None]


def _eval_extraction(args: argparse.Namespace) -> int:
    """Score triple extraction before any graph write.

    Each JSONL line: {"text", "triples": [Triple, ...]}.
    """

    settings = get_settings()
    setup_telemetry(settings)
    ontology = Ontology.load(settings.ontology_path) if settings.ontology_path else None
    ingestor = KnowledgeGraphIngestor(
        llm=build_chat_model(settings, role="extraction"),
        detector=NoopDetector(),
        limits=IngestionLimits.from_settings(settings),
        targets=IngestionTargets(
            chunk_store=LocalChunkStore(build_embeddings(settings)), graph_writer=None
        ),
        aids=ExtractionAids(ontology=ontology),
    )
    totals: dict[str, list] = {"predicted": [], "gold": []}
    failed = 0
    for n, line in enumerate(
        Path(args.dataset).read_text(encoding="utf-8").splitlines()
    ):
        if not line.strip():
            continue
        case = json.loads(line)
        try:
            predicted, _ = ingestor.extract_triples(
                case["text"], DocumentMetadata(source_id=f"case-{n}")
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # One case must not end the run; the row says why it has no scores.
            failed += 1
            _emit({"case": n, "error": type(exc).__name__}, "extraction case failed")
            continue
        pred = {grounded_key(t) for t in predicted}
        gold = {triple_key(Triple.model_validate(t), ontology) for t in case["triples"]}
        totals["predicted"] += [(n, *k) for k in pred]
        totals["gold"] += [(n, *k) for k in gold]
        _emit(
            {"case": n, **extraction_scores(pred, gold)},
            "extraction case scored",
            indent=None,
        )
    _emit(
        {"case": "overall", **extraction_scores(totals["predicted"], totals["gold"])},
        "extraction overall",
        indent=None,
    )
    return 1 if failed else 0


def _graph(args: argparse.Namespace) -> int:
    """Mermaid diagram of the LangGraph workflow, generated from the compiled graph."""

    diagram = workflow_mermaid()
    if args.output:
        Path(args.output).write_text(diagram + "\n", encoding="utf-8")
        log.info("wrote workflow diagram to %s", args.output)
    else:
        _emit({"mermaid": diagram}, "workflow diagram")
    return 0


@contextmanager
def _open_chunk_store(settings) -> Iterator[Neo4jChunkStore]:
    if settings.chunk_store != "neo4j":
        raise SystemExit(
            "nexo chunks works on the neo4j chunk store (CHUNK_STORE=neo4j)"
        )
    with GraphStore.from_settings(settings, read_only=False) as graph:
        store = chunk_store_from_settings(settings, graph)
        assert isinstance(store, Neo4jChunkStore)
        yield store


def _chunks_status(_args: argparse.Namespace) -> int:
    """How many of the graph's chunks have no text (ingested before ADR-0018)."""
    settings = get_settings()
    with _open_chunk_store(settings) as store:
        missing = store.missing_text()
    result = {"chunk_store": settings.chunk_store, "chunks_without_text": missing}
    if missing:
        _emit(
            result,
            "%d chunks in the graph have no text: nexo chunks import (from %s) or re-ingest",
            missing,
            settings.vector_store_path,
            level=logging.WARNING,
        )
        return 1
    _emit(result, "every chunk in the graph has its text")
    return 0


def _chunks_import(args: argparse.Namespace) -> int:
    """Copy a local JSON chunk store into Neo4j, with its stored embeddings."""
    settings = get_settings()
    with _open_chunk_store(settings) as store:
        store.ensure_schema()
        result = import_local_store(
            store, Path(args.path or settings.vector_store_path)
        )
        result["chunks_without_text"] = store.missing_text()
    _emit(
        result,
        "imported %d chunks (%d referenced by the graph); %d graph chunks still lack text",
        result["imported"],
        result["referenced_by_graph"],
        result["chunks_without_text"],
    )
    return 0


def _add_chunk_commands(sub: argparse._SubParsersAction) -> None:
    """`nexo chunks status|import`: the chunk store in Neo4j (ADR-0018)."""
    chunks = sub.add_parser("chunks", help="the chunk store in Neo4j: status, import")
    verbs = chunks.add_subparsers(dest="chunks_command", required=True)
    verbs.add_parser(
        "status", help="count graph chunks that have no text"
    ).set_defaults(handler=_chunks_status)
    importer = verbs.add_parser(
        "import",
        help="copy a local JSON chunk store (.data/vector_store.json) into Neo4j",
    )
    importer.add_argument("path", nargs="?", help="default: VECTOR_STORE_PATH")
    importer.set_defaults(handler=_chunks_import)


def _context_problems(settings) -> list[str]:
    """Ollama servers whose loaded models run with less context than their profile sets."""
    servers = []
    if settings.local_profile:
        role_set = "light" if settings.remote_profile else "single"
        servers.append((settings.local_base_url, settings.local_profile, role_set))
    if settings.remote_profile:
        servers.append((settings.remote_base_url, settings.remote_profile, "heavy"))
    return [
        problem
        for server in servers
        if (problem := context_problem(*server)) is not None
    ]


def _models(args: argparse.Namespace) -> int:
    """Which model and server each role uses, after profiles and explicit settings."""

    settings = get_settings()
    rows = resolved_models(settings)
    if args.check:
        rows = check_model_servers(settings, rows)
        for problem in _context_problems(settings):
            log.warning("%s", problem)
    failed = [row.role for row in rows if (row.check or "ok") != "ok"]
    profiles = (
        f"LOCAL_PROFILE={settings.local_profile or '-'} "
        f"REMOTE_PROFILE={settings.remote_profile or '-'}"
    )
    result = {
        "local_profile": settings.local_profile,
        "remote_profile": settings.remote_profile,
        "roles": [row.as_dict() for row in rows],
    }
    if failed:
        _emit(
            result,
            "models (%s): check failed for %s",
            profiles,
            ", ".join(failed),
            level=logging.ERROR,
        )
        return 1
    _emit(
        result,
        "models (%s)%s",
        profiles,
        ": all reachable" if args.check else "",
    )
    return 0


def _config(args: argparse.Namespace) -> int:
    """Each effective setting and where it came from; secret values are never shown."""
    rows = explain(get_settings(), everything=args.all)
    result = {
        row.name: (
            {"value": row.value, "source": row.source} if args.explain else row.value
        )
        for row in rows
    }
    _emit(result, "settings%s", " and their sources" if args.explain else "")
    return 0


def _mcp(args: argparse.Namespace) -> int:
    """Serve the knowledge graph and chunk store as read-only MCP tools."""

    settings = get_settings()
    setup_telemetry(settings)
    with ExitStack() as stack:
        graph = stack.enter_context(GraphStore.from_settings(settings, read_only=True))
        chunks = chunk_store_from_settings(settings, graph)
        runtime = (
            stack.enter_context(open_review_runtime(settings, chunks))
            if settings.mcp_review_tools
            else None
        )
        # The same read-time masking as the agent, with or without the review
        # tools (with them, build_mcp_server uses the runtime's redactor).
        redactor = (
            None
            if runtime is not None
            else ChunkRedactor(detector_from_settings(settings))
        )
        server = build_mcp_server(
            graph=graph,
            chunks=chunks,
            settings=settings,
            runtime=runtime,
            redactor=redactor,
        )
        log.info("serving MCP over %s", args.transport)
        if args.transport == "stdio":
            server.run(transport="stdio")
        else:  # no authentication: keep it on localhost or behind an authenticating proxy
            server.run(transport="streamable-http", host=args.host, port=args.port)
    return 0


def _review_inbox(args: argparse.Namespace) -> int:
    """Serve the Agent Inbox adapter: paused questions reviewed in LangChain's Agent Inbox."""

    settings = get_settings()
    port = args.port or settings.review_inbox_port
    inbox_port = args.inbox_port or settings.agent_inbox_port
    clearance = settings.effective_mcp_clearance
    with open_runtime(settings) as runtime:
        adapter = InboxAdapter(
            runtime,
            reviewer=caller_attributes(settings, None)["user.id"],
            clearance=clearance,
        )
        server = build_server(adapter, port=port, inbox_port=inbox_port)
        log.info(
            "Agent Inbox adapter on http://127.0.0.1:%s for http://127.0.0.1:%s "
            "(reviewer %s, clearance %s)",
            port,
            inbox_port,
            adapter.reviewer,
            clearance,
        )
        _serve(server)
    return 0


def _add_document_commands(sub: argparse._SubParsersAction) -> None:
    """`ingest`, `ask`, `review` and `status`: documents in, answers out."""
    ingest = sub.add_parser(
        "ingest", help="chunk, embed and extract a text/Markdown document"
    )
    ingest.add_argument("file")
    ingest.add_argument("--source-id")
    ingest.add_argument("--title")
    ingest.add_argument("--doc-type", default="general")
    ingest.add_argument("--source-uri")
    ingest.add_argument("--effective-date", help="YYYY-MM-DD")
    ingest.add_argument(
        "--sensitivity",
        default="internal",
        choices=CLEARANCES,
    )
    ingest.set_defaults(handler=_ingest)

    ask = sub.add_parser(
        "ask", help="start an investigation (pauses for review per spec.review)"
    )
    ask.add_argument("query")
    ask.add_argument("--thread-id")
    ask.add_argument("--clearance", choices=CLEARANCES)
    ask.add_argument(
        "--user", help="who is asking, on the trace (default: NEXO_USER or login name)"
    )
    ask.set_defaults(handler=_ask)

    review = sub.add_parser("review", help="approve or reject a paused investigation")
    review.add_argument("thread_id")
    verdict = review.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--approve", action="store_true")
    verdict.add_argument("--reject", action="store_true")
    review.add_argument("--reviewer", required=True)
    review.add_argument("--comment")
    review.add_argument("--acknowledge-findings", action="store_true")
    review.set_defaults(handler=_review)

    status = sub.add_parser("status", help="show a thread's state")
    status.add_argument("thread_id")
    status.set_defaults(handler=_status)

    delete = sub.add_parser(
        "delete", help="remove a document and what only it supported (operators only)"
    )
    delete.add_argument("source_id")
    delete.set_defaults(handler=_delete)


def main(argv: list[str] | None = None) -> int:
    """Parse the command line, run the chosen subcommand and return its exit code."""
    parser = argparse.ArgumentParser(prog="nexo")
    sub = parser.add_subparsers(dest="command", required=True)

    _add_document_commands(sub)

    evaluate = sub.add_parser(
        "eval", help="run the investigator over a JSONL golden set"
    )
    evaluate.add_argument("dataset")
    evaluate.add_argument(
        "--dataset-name", help="Langfuse dataset (default: the file's name)"
    )
    evaluate.add_argument(
        "--run-name", help="Langfuse experiment (default: agent model and time)"
    )
    evaluate.add_argument(
        "--repeat",
        type=int,
        default=1,
        metavar="N",
        help="run each case N times and add a mean/spread summary row per question",
    )
    evaluate.set_defaults(handler=_eval)

    evaluate_extraction = sub.add_parser(
        "eval-extraction", help="score triple extraction against gold triples"
    )
    evaluate_extraction.add_argument("dataset")
    evaluate_extraction.set_defaults(handler=_eval_extraction)

    review_ui = sub.add_parser(
        "review-ui",
        help="serve the demo review page (no authentication, loopback only)",
    )
    review_ui.add_argument("--host", default="127.0.0.1")
    review_ui.add_argument("--port", type=int, default=8766)
    review_ui.set_defaults(handler=_review_ui)

    graph = sub.add_parser("graph", help="Mermaid diagram of the LangGraph workflow")
    graph.add_argument(
        "--output", help="write the diagram to this file (e.g. workflow.mmd)"
    )
    graph.set_defaults(handler=_graph)

    models = sub.add_parser("models", help="show the model and server each role uses")
    models.add_argument(
        "--check",
        action="store_true",
        help="ask each server whether it serves the model",
    )
    models.set_defaults(handler=_models)

    serve = sub.add_parser(
        "mcp", help="serve the knowledge graph as read-only MCP tools"
    )
    serve.add_argument(
        "--transport", choices=["stdio", "streamable-http"], default="stdio"
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(handler=_mcp)

    inbox = sub.add_parser(
        "review-inbox", help="serve the Agent Inbox adapter (opt-in review page)"
    )
    inbox.add_argument("--port", type=int, help="adapter port (REVIEW_INBOX_PORT)")
    inbox.add_argument(
        "--inbox-port", type=int, help="the inbox's port, the only allowed origin"
    )
    inbox.set_defaults(handler=_review_inbox)

    config = sub.add_parser(
        "config", help="print the effective settings (secret values redacted)"
    )
    config.add_argument(
        "--explain",
        action="store_true",
        help="name each value's source: environment, secrets store, rendered env, .env",
    )
    config.add_argument("--all", action="store_true", help="include defaults")
    config.set_defaults(handler=_config)

    add_setup_parser(sub)
    _add_chunk_commands(sub)

    args = parser.parse_args(argv)
    # Logs go to stderr: stdout is the command's JSON, or the protocol for MCP over stdio.
    configure_logging(get_settings())
    # setup renders before it reads; everything else would run on stale settings.
    if args.command != "setup" and (drift := render_drift()):
        log.warning("%s", drift)
    try:
        return args.handler(args)
    finally:
        shutdown_telemetry()


if __name__ == "__main__":
    sys.exit(main())
