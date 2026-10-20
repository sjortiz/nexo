"""Command line entry point: ingest, ask, review, status, eval, models."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

from nexo.agents.models import InvestigationScope
from nexo.config import get_settings
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
from nexo.llm import (
    build_chat_model,
    build_embeddings,
    check_model_servers,
    resolved_models,
)
from nexo.logging_setup import configure_logging
from nexo.mcp_server import build_mcp_server
from nexo.orchestration.graph import workflow_mermaid
from nexo.setup.commands import add_parser as add_setup_parser
from nexo.service import (
    build_investigator,
    chunk_store_from_settings,
    open_ingestor,
    open_runtime,
)
from nexo.storage.graph_store import GraphStore
from nexo.storage.neo4j_chunk_store import Neo4jChunkStore, import_local_store
from nexo.storage.ingest import ExtractionAids, IngestionTargets, KnowledgeGraphIngestor
from nexo.storage.ontology import Ontology
from nexo.storage.schemas import DocumentMetadata, Sensitivity, Triple
from nexo.storage.sensitive_data import NoopDetector
from nexo.storage.vector_store import ChunkFilter, LocalChunkStore
from nexo.telemetry import setup_telemetry, shutdown_telemetry, traced_span

log = logging.getLogger("nexo.cli")


def _emit(message: str, model) -> None:
    log.info(
        message, extra={"result": model.model_dump(mode="json", exclude_none=True)}
    )


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
        log.warning(
            "ingested %s with %d failed chunks",
            report.source_id,
            len(report.failed_chunk_ids),
            extra={"result": report.model_dump(mode="json")},
        )
        return 1
    _emit(f"ingested {report.source_id}", report)
    return 0


def _ask(args: argparse.Namespace) -> int:

    with open_runtime(get_settings()) as runtime:
        result = runtime.start(
            args.query,
            thread_id=args.thread_id,
            clearance=args.clearance,
            user=args.user,
        )
        _emit(f"thread {result.thread_id}: {result.status}", result)
    return 0


def _review(args: argparse.Namespace) -> int:

    decision = {
        "approval_status": "approved" if args.approve else "rejected",
        "reviewer": args.reviewer,
        "comment": args.comment,
        "acknowledged_findings": args.acknowledge_findings,
    }
    with open_runtime(get_settings()) as runtime:
        result = runtime.review(args.thread_id, decision)
        _emit(
            f"thread {result.thread_id}: {result.status} ({result.approval_status})",
            result,
        )
    return 0


def _status(args: argparse.Namespace) -> int:

    with open_runtime(get_settings()) as runtime:
        result = runtime.status(args.thread_id)
        _emit(f"thread {result.thread_id}: {result.status}", result)
    return 0


def _eval(args: argparse.Namespace) -> int:
    """Each JSONL line: {"question", "relevant_chunk_ids": [...], "reference_answer"?,
    "answer_contains"?: [...], "strategy"?: "graph"|"vector"|"hybrid",
    "clearance"?: "public"|"internal"|"pii"|"phi"}.

    Eval skips the router, so `strategy` replays its choice (default hybrid) and `clearance`
    stands for the caller's (default DEFAULT_CLEARANCE).
    """

    settings = get_settings()
    setup_telemetry(settings)
    judge = None
    if settings.judge_model:
        judge = build_chat_model(settings, role="judge")
    graph = GraphStore.from_settings(settings, read_only=True)
    chunks = chunk_store_from_settings(settings, graph)
    investigator = build_investigator(settings, graph, chunks)
    try:
        for line in Path(args.dataset).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            case = json.loads(line)
            with traced_span("eval.case"):
                output = investigator.investigate(
                    case["question"],
                    scope=InvestigationScope(
                        strategy=case.get("strategy", "hybrid"),
                        chunk_filter=ChunkFilter(
                            max_sensitivity=case.get(
                                "clearance", settings.default_clearance
                            )
                        ),
                    ),
                )
                metrics = retrieval_scores(output, case.get("relevant_chunk_ids", []))
                if "answer_contains" in case:
                    metrics.update(
                        answer_scores(output.answer, case["answer_contains"])
                    )
                if judge and output.answer:
                    evidence = [c.text for c in chunks.get_by_ids(output.citations)]
                    metrics["generation.faithfulness"] = judge_faithfulness(
                        judge, output.answer, evidence
                    ).score
                    metrics["generation.relevancy"] = judge_relevancy(
                        judge, case["question"], output.answer
                    ).score
                record_metrics(metrics)
            log.info(
                "eval case scored",
                extra={
                    "result": {
                        "question": case["question"],
                        **metrics,
                        "answer": output.answer,
                        "tool_calls": len(output.tool_calls),
                        "stop_reason": output.stop_reason,
                    }
                },
            )
    finally:
        graph.close()
    return 0


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
        settings=settings,
        targets=IngestionTargets(
            chunk_store=LocalChunkStore(build_embeddings(settings)), graph_writer=None
        ),
        aids=ExtractionAids(ontology=ontology),
    )
    totals: dict[str, list] = {"predicted": [], "gold": []}
    for n, line in enumerate(
        Path(args.dataset).read_text(encoding="utf-8").splitlines()
    ):
        if not line.strip():
            continue
        case = json.loads(line)
        predicted, _ = ingestor.extract_triples(
            case["text"], DocumentMetadata(source_id=f"case-{n}")
        )
        pred = {grounded_key(t) for t in predicted}
        gold = {triple_key(Triple.model_validate(t), ontology) for t in case["triples"]}
        totals["predicted"] += [(n, *k) for k in pred]
        totals["gold"] += [(n, *k) for k in gold]
        log.info(
            "extraction case scored",
            extra={"result": {"case": n, **extraction_scores(pred, gold)}},
        )
    log.info(
        "extraction overall",
        extra={"result": extraction_scores(totals["predicted"], totals["gold"])},
    )
    return 0


def _graph(args: argparse.Namespace) -> int:
    """Mermaid diagram of the LangGraph workflow, generated from the compiled graph."""

    diagram = workflow_mermaid()
    if args.output:
        Path(args.output).write_text(diagram + "\n", encoding="utf-8")
        log.info("wrote workflow diagram to %s", args.output)
    else:
        log.info("workflow diagram", extra={"result": {"mermaid": diagram}})
    return 0


def _chunk_store(settings) -> tuple[GraphStore, Neo4jChunkStore]:
    if settings.chunk_store != "neo4j":
        raise SystemExit(
            "nexo chunks works on the neo4j chunk store (CHUNK_STORE=neo4j)"
        )
    graph = GraphStore.from_settings(settings, read_only=False)
    store = chunk_store_from_settings(settings, graph)
    assert isinstance(store, Neo4jChunkStore)
    return graph, store


def _chunks_status(_args: argparse.Namespace) -> int:
    """How many of the graph's chunks have no text (ingested before ADR-0018)."""
    settings = get_settings()
    graph, store = _chunk_store(settings)
    try:
        missing = store.missing_text()
    finally:
        graph.close()
    result = {"chunk_store": settings.chunk_store, "chunks_without_text": missing}
    if missing:
        log.warning(
            "%d chunks in the graph have no text: nexo chunks import (from %s) or re-ingest",
            missing,
            settings.vector_store_path,
            extra={"result": result},
        )
        return 1
    log.info("every chunk in the graph has its text", extra={"result": result})
    return 0


def _chunks_import(args: argparse.Namespace) -> int:
    """Copy a local JSON chunk store into Neo4j, with its stored embeddings."""
    settings = get_settings()
    graph, store = _chunk_store(settings)
    try:
        store.ensure_schema()
        result = import_local_store(
            store, Path(args.path or settings.vector_store_path)
        )
        result["chunks_without_text"] = store.missing_text()
    finally:
        graph.close()
    log.info(
        "imported %d chunks (%d referenced by the graph); %d graph chunks still lack text",
        result["imported"],
        result["referenced_by_graph"],
        result["chunks_without_text"],
        extra={"result": result},
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


def _models(args: argparse.Namespace) -> int:
    """Which model and server each role uses, after profiles and explicit settings."""

    settings = get_settings()
    rows = resolved_models(settings)
    if args.check:
        rows = check_model_servers(settings, rows)
    failed = [row["role"] for row in rows if row.get("check", "ok") != "ok"]
    profiles = (
        f"LOCAL_PROFILE={settings.local_profile or '-'} "
        f"REMOTE_PROFILE={settings.remote_profile or '-'}"
    )
    result = {
        "local_profile": settings.local_profile,
        "remote_profile": settings.remote_profile,
        "roles": rows,
    }
    if failed:
        log.error(
            "models (%s): check failed for %s",
            profiles,
            ", ".join(failed),
            extra={"result": result},
        )
        return 1
    log.info(
        "models (%s)%s",
        profiles,
        ": all reachable" if args.check else "",
        extra={"result": result},
    )
    return 0


def _mcp(args: argparse.Namespace) -> int:
    """Serve the knowledge graph and chunk store as read-only MCP tools."""

    settings = get_settings()
    setup_telemetry(settings)
    graph = GraphStore.from_settings(settings, read_only=True)
    chunks = chunk_store_from_settings(settings, graph)
    server = build_mcp_server(graph=graph, chunks=chunks, settings=settings)
    log.info("serving MCP over %s", args.transport)
    try:
        if args.transport == "stdio":
            server.run(transport="stdio")
        else:  # no authentication: keep it on localhost or behind an authenticating proxy
            server.run(transport="streamable-http", host=args.host, port=args.port)
    finally:
        graph.close()
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
        choices=["public", "internal", "pii", "phi"],
    )
    ingest.set_defaults(handler=_ingest)

    ask = sub.add_parser("ask", help="start an investigation (pauses for review)")
    ask.add_argument("query")
    ask.add_argument("--thread-id")
    ask.add_argument("--clearance", choices=["public", "internal", "pii", "phi"])
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


def main(argv: list[str] | None = None) -> int:
    """Parse the command line, run the chosen subcommand and return its exit code."""
    parser = argparse.ArgumentParser(prog="nexo")
    sub = parser.add_subparsers(dest="command", required=True)

    _add_document_commands(sub)

    evaluate = sub.add_parser(
        "eval", help="run the investigator over a JSONL golden set"
    )
    evaluate.add_argument("dataset")
    evaluate.set_defaults(handler=_eval)

    evaluate_extraction = sub.add_parser(
        "eval-extraction", help="score triple extraction against gold triples"
    )
    evaluate_extraction.add_argument("dataset")
    evaluate_extraction.set_defaults(handler=_eval_extraction)

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

    add_setup_parser(sub)
    _add_chunk_commands(sub)

    args = parser.parse_args(argv)
    # MCP over stdio uses stdout for the protocol: logs go to stderr there.
    configure_logging(
        get_settings(), stream=sys.stderr if args.command == "mcp" else None
    )
    try:
        return args.handler(args)
    finally:
        shutdown_telemetry()


if __name__ == "__main__":
    sys.exit(main())
