"""Command line entry point: ingest, ask, review, status, eval."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

from nexo.config import get_settings
from nexo.logging_setup import configure_logging
from nexo.telemetry import shutdown_telemetry

log = logging.getLogger("nexo.cli")


def _emit(message: str, model) -> None:
    log.info(message, extra={"result": model.model_dump(mode="json", exclude_none=True)})


def _ingest(args: argparse.Namespace) -> int:
    from nexo.service import open_ingestor
    from nexo.storage.schemas import DocumentMetadata, Sensitivity

    metadata = DocumentMetadata(
        source_id=args.source_id or Path(args.file).stem,
        title=args.title or Path(args.file).stem,
        doc_type=args.doc_type,
        source_uri=args.source_uri or Path(args.file).resolve().as_uri(),
        effective_date=date.fromisoformat(args.effective_date) if args.effective_date else None,
        sensitivity=Sensitivity(args.sensitivity),
    )
    with open_ingestor(get_settings()) as ingestor:
        report = ingestor.ingest(Path(args.file).read_text(encoding="utf-8"), metadata)
    if report.failed_chunk_ids:
        log.warning("ingested %s with %d failed chunks", report.source_id, len(report.failed_chunk_ids),
                    extra={"result": report.model_dump(mode="json")})
        return 1
    _emit(f"ingested {report.source_id}", report)
    return 0


def _ask(args: argparse.Namespace) -> int:
    from nexo.service import open_runtime

    with open_runtime(get_settings()) as runtime:
        result = runtime.start(args.query, thread_id=args.thread_id, clearance=args.clearance)
        _emit(f"thread {result.thread_id}: {result.status}", result)
    return 0


def _review(args: argparse.Namespace) -> int:
    from nexo.service import open_runtime

    decision = {
        "approval_status": "approved" if args.approve else "rejected",
        "reviewer": args.reviewer,
        "comment": args.comment,
        "acknowledged_findings": args.acknowledge_findings,
    }
    with open_runtime(get_settings()) as runtime:
        result = runtime.review(args.thread_id, decision)
        _emit(f"thread {result.thread_id}: {result.status} ({result.approval_status})", result)
    return 0


def _status(args: argparse.Namespace) -> int:
    from nexo.service import open_runtime

    with open_runtime(get_settings()) as runtime:
        result = runtime.status(args.thread_id)
        _emit(f"thread {result.thread_id}: {result.status}", result)
    return 0


def _eval(args: argparse.Namespace) -> int:
    """Each JSONL line: {"question", "relevant_chunk_ids": [...], "reference_answer"?}."""
    from nexo.agents.investigator import Investigator
    from nexo.evaluation import judge_faithfulness, judge_relevancy, record_metrics, retrieval_scores
    from nexo.llm import build_chat_model, build_embeddings, build_strands_model
    from nexo.storage.graph_store import GraphStore
    from nexo.storage.vector_store import LocalChunkStore
    from nexo.telemetry import setup_telemetry, traced_span

    settings = get_settings()
    setup_telemetry(settings)
    judge = None
    if settings.judge_model:
        judge = build_chat_model(settings, role="judge")
    graph = GraphStore.from_settings(settings, read_only=True)
    chunks = LocalChunkStore(build_embeddings(settings), settings.vector_store_path, mode=settings.retrieval_mode)
    investigator = Investigator(model=build_strands_model(settings), graph=graph, chunks=chunks,
                                settings=settings, schema_text=graph.describe_schema())
    try:
        for line in Path(args.dataset).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            case = json.loads(line)
            with traced_span("eval.case"):
                output = investigator.investigate(case["question"])
                metrics = retrieval_scores(output, case.get("relevant_chunk_ids", []))
                if judge and output.answer:
                    evidence = [c.text for c in chunks.get_by_ids(output.citations)]
                    metrics["generation.faithfulness"] = judge_faithfulness(judge, output.answer, evidence).score
                    metrics["generation.relevancy"] = judge_relevancy(judge, case["question"], output.answer).score
                record_metrics(metrics)
            log.info("eval case scored", extra={"result": {"question": case["question"], **metrics}})
    finally:
        graph.close()
    return 0


def _eval_extraction(args: argparse.Namespace) -> int:
    """Each JSONL line: {"text", "triples": [Triple, ...]} - scores extraction before any graph write."""
    from nexo.evaluation import extraction_scores, grounded_key, triple_key
    from nexo.llm import build_chat_model, build_embeddings
    from nexo.storage.ingest import KnowledgeGraphIngestor
    from nexo.storage.ontology import Ontology
    from nexo.storage.schemas import DocumentMetadata, Triple
    from nexo.storage.sensitive_data import NoopDetector
    from nexo.storage.vector_store import LocalChunkStore
    from nexo.telemetry import setup_telemetry

    settings = get_settings()
    setup_telemetry(settings)
    ontology = Ontology.load(settings.ontology_path) if settings.ontology_path else None
    ingestor = KnowledgeGraphIngestor(
        llm=build_chat_model(settings, role="extraction"), chunk_store=LocalChunkStore(build_embeddings(settings)),
        graph_writer=None, detector=NoopDetector(), settings=settings, ontology=ontology,
    )
    totals: dict[str, list] = {"predicted": [], "gold": []}
    for n, line in enumerate(Path(args.dataset).read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        case = json.loads(line)
        predicted, _ = ingestor.extract_triples(case["text"], DocumentMetadata(source_id=f"case-{n}"))
        pred = {grounded_key(t) for t in predicted}
        gold = {triple_key(Triple.model_validate(t), ontology) for t in case["triples"]}
        totals["predicted"] += [(n, *k) for k in pred]
        totals["gold"] += [(n, *k) for k in gold]
        log.info("extraction case scored", extra={"result": {"case": n, **extraction_scores(pred, gold)}})
    log.info("extraction overall", extra={"result": extraction_scores(totals["predicted"], totals["gold"])})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nexo")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="chunk, embed and extract a text/Markdown document")
    ingest.add_argument("file")
    ingest.add_argument("--source-id")
    ingest.add_argument("--title")
    ingest.add_argument("--doc-type", default="general")
    ingest.add_argument("--source-uri")
    ingest.add_argument("--effective-date", help="YYYY-MM-DD")
    ingest.add_argument("--sensitivity", default="internal", choices=["public", "internal", "pii", "phi"])
    ingest.set_defaults(handler=_ingest)

    ask = sub.add_parser("ask", help="start an investigation (pauses for review)")
    ask.add_argument("query")
    ask.add_argument("--thread-id")
    ask.add_argument("--clearance", choices=["public", "internal", "pii", "phi"])
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

    evaluate = sub.add_parser("eval", help="run the investigator over a JSONL golden set")
    evaluate.add_argument("dataset")
    evaluate.set_defaults(handler=_eval)

    evaluate_extraction = sub.add_parser("eval-extraction", help="score triple extraction against gold triples")
    evaluate_extraction.add_argument("dataset")
    evaluate_extraction.set_defaults(handler=_eval_extraction)

    args = parser.parse_args(argv)
    configure_logging(get_settings())
    try:
        return args.handler(args)
    finally:
        shutdown_telemetry()


if __name__ == "__main__":
    sys.exit(main())
