"""Neo4j access over the Bolt driver.

Two layers share one graph (the "lexical graph" + "domain graph" pattern of
Neo4j's LLM Graph Builder):

    (:Document {source_id, title, doc_type, source_uri, effective_date, sensitivity})
    (:Document)-[:FIRST_CHUNK]->(:Chunk)-[:NEXT_CHUNK]->(:Chunk)-[:PART_OF]->(:Document)
    (:Entity:<EntityType>[:PII][:PHI])-[:MENTIONED_IN {surface, resolution}]->(:Chunk)
    (:Entity)-[:<RELATION> {chunk_id, chunk_ids, evidence, evidence_rank, certainty,
                            valid_at, extracted_as}]->(:Entity)

`chunk_id` is the chunk where an entity was first extracted, and for an edge
the chunk its `evidence` quotes, with `evidence_rank` that chunk's rank; when
that chunk stops supporting the edge the quote goes, and the next chunk that
states the edge supplies one. `chunk_ids` accumulates every supporting chunk
across documents. Derived
properties are recomputed for every touched entity and edge on each replace:
entity `aliases` from the surfaces on MENTIONED_IN (withdrawing a document
withdraws the aliases and merges it contributed), and `sensitivity_rank` as
the *least* sensitive supporting chunk - something is visible to a caller if
any source they are cleared for supports it. The :PII/:PHI labels follow.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, LiteralString, NamedTuple, Protocol, cast
from urllib.parse import urlparse

from neo4j import (
    READ_ACCESS,
    WRITE_ACCESS,
    Driver,
    GraphDatabase,
    ManagedTransaction,
    Session,
    unit_of_work,
)
from neo4j.exceptions import DriverError, Neo4jError, ServiceUnavailable, SessionExpired
from opentelemetry.trace import SpanKind
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from nexo.config import Settings
from nexo.storage.ontology import FALLBACK_RELATION
from nexo.storage.resolution import Candidate
from nexo.storage.schemas import (
    SourceSwap,
    RELATION_RE,
    ChunkRecord,
    DocumentMetadata,
    EntityType,
    GroundedTriple,
    Sensitivity,
)
from nexo.telemetry import traced_span

DATA_MODEL = (
    """\
(:Document {source_id, title, doc_type, source_uri, effective_date, sensitivity})
(:Document)-[:FIRST_CHUNK]->(:Chunk {chunk_id, source_id, ordinal, section_path, sensitivity})
(:Chunk)-[:NEXT_CHUNK]->(:Chunk), (:Chunk)-[:PART_OF]->(:Document)
(:Entity:<EntityType>[:PII][:PHI] {key, name, aliases, type, description, \
chunk_id, chunk_ids, source_ids,
                                  sensitivity_rank, proposed_codes_json})
(:Entity)-[:MENTIONED_IN {surface, resolution}]->(:Chunk)
(:Entity)-[:<RELATION> {chunk_id, chunk_ids, evidence, evidence_rank, certainty, valid_at, \
extracted_as, sensitivity_rank}]->(:Entity)
(:Entity)-[:RELATED_TO {predicate, ...same properties}]->(:Entity)   \
# predicate outside the ontology
EntityType is one of: """
    + ", ".join(t.value for t in EntityType)
    + """
sensitivity_rank (least sensitive supporting source): """
    + " ".join(f"{level.rank}={level}" for level in Sensitivity)
)

# Connection drops surface as ServiceUnavailable/SessionExpired. Transient
# server errors inside managed transactions are already retried by the driver.
_connection_retry = retry(
    retry=retry_if_exception_type((ServiceUnavailable, SessionExpired)),
    wait=wait_exponential_jitter(multiplier=0.5, max=8),
    stop=stop_after_attempt(4),
    reraise=True,
)

_SCHEMA_STATEMENTS: tuple[LiteralString, ...] = (
    "CREATE CONSTRAINT entity_key IF NOT EXISTS FOR (e:Entity) REQUIRE e.key IS UNIQUE",
    "CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.chunk_id IS UNIQUE",
    "CREATE CONSTRAINT document_source_id IF NOT EXISTS FOR (d:Document) "
    "REQUIRE d.source_id IS UNIQUE",
    "CREATE INDEX entity_chunk_id IF NOT EXISTS FOR (e:Entity) ON (e.chunk_id)",
    "CREATE INDEX entity_type IF NOT EXISTS FOR (e:Entity) ON (e.type)",
    # Text index: serves CONTAINS lookups for agent queries and resolution blocking.
    "CREATE TEXT INDEX entity_name_text IF NOT EXISTS FOR (e:Entity) ON (e.name)",
)


@dataclass(frozen=True)
class ReadResult:
    """Rows of a read query, and whether they were cut at the row limit."""

    rows: list[dict[str, Any]]
    truncated: bool


class GraphReader(Protocol):
    """Read-only graph access: what the agent tools and the MCP server need."""

    def run_read_query(
        self, cypher: str, parameters: dict[str, Any] | None = None
    ) -> ReadResult:
        """Run a read-only Cypher query and return its bounded rows."""

    def describe_schema(self) -> str:
        """Describe the graph's labels and relationship patterns as text."""


@dataclass(frozen=True)
class QueryLimits:
    """Bounds on agent-authored queries, and whether spans may record their text."""

    timeout_s: float
    max_rows: int
    capture_text: bool = False


class GraphStore:
    """Reads and writes the lexical and domain graph in Neo4j."""

    def __init__(
        self,
        driver: Driver,
        *,
        database: str,
        limits: QueryLimits,
        uri: str | None = None,
    ) -> None:
        self._driver = driver
        self._database = database
        self._limits = limits
        address = urlparse(uri or "")
        self._server = {"server.address": address.hostname, "server.port": address.port}

    @classmethod
    def from_settings(cls, settings: Settings, *, read_only: bool) -> GraphStore:
        """Connect with the configured driver, using the read-only user when asked."""
        user, password = settings.neo4j_user, settings.neo4j_password
        if read_only and settings.neo4j_read_user and settings.neo4j_read_password:
            user, password = settings.neo4j_read_user, settings.neo4j_read_password
        driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(user, password.get_secret_value()),
            connection_timeout=10,
            max_transaction_retry_time=30,
        )
        return cls(
            driver,
            database=settings.neo4j_database,
            limits=QueryLimits(
                timeout_s=settings.neo4j_query_timeout_s,
                max_rows=settings.neo4j_max_rows,
                capture_text=settings.capture_content,
            ),
            uri=settings.neo4j_uri,
        )

    def close(self) -> None:
        """Close the driver and its connections."""
        self._driver.close()

    def __enter__(self) -> GraphStore:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def session(self, *, write: bool) -> Session:
        """A session on this store's database (the chunk store shares the driver)."""
        return self._driver.session(
            database=self._database,
            default_access_mode=WRITE_ACCESS if write else READ_ACCESS,
        )

    def _span(self, query_summary: str, query_text: str | None = None):
        """CLIENT span per the OTEL database conventions (db.system.name=neo4j)."""
        attributes = {
            "db.system.name": "neo4j",
            "db.namespace": self._database,
            "db.query.summary": query_summary,
            **self._server,
        }
        if query_text is not None and self._limits.capture_text:
            # Agent-authored Cypher is not parameterized and may carry names: opt-in only.
            attributes["db.query.text"] = query_text[:4000]
        return traced_span(query_summary, attributes, kind=SpanKind.CLIENT)

    @_connection_retry
    def verify(self) -> None:
        """Fail fast if the database is unreachable."""
        self._driver.verify_connectivity()

    @_connection_retry
    def ensure_schema(self) -> None:
        """Create the constraints and indexes the graph relies on, if missing."""
        with self.session(write=True) as session:
            for statement in _SCHEMA_STATEMENTS:
                session.run(statement).consume()

    # --- writes (ingestion) ------------------------------------------------------
    @_connection_retry
    def replace_source(
        self,
        document: DocumentMetadata,
        chunks: Sequence[ChunkRecord],
        triples: Sequence[GroundedTriple],
    ) -> SourceSwap:
        """Swap a document's lexical and domain subgraph in one transaction.

        Detaches the previous version's provenance (dropping what only it
        supported) and its stale chunks, writes Document/Chunk nodes, then upserts
        the new triples. Current chunks keep their nodes, so text and embeddings
        stored on them (the Neo4j chunk store) survive the swap.
        """
        plan = _plan_writes(triples)
        keep = sorted(c.chunk_id for c in chunks)

        def work(tx: ManagedTransaction) -> int:
            affected, removed = _delete_source(tx, document.source_id, keep)
            if chunks:
                _write_lexical(tx, document, chunks)
            if triples:
                _write_domain(tx, plan)
            _refresh_derived(tx, sorted(affected | {m["key"] for m in plan.mentions}))
            return removed

        with self._span("replace_source"), self.session(write=True) as session:
            removed = session.execute_write(work)
        return SourceSwap(triples_written=len(triples), chunks_removed=removed)

    @_connection_retry
    def find_candidates(
        self, entity_type: EntityType, tokens: Sequence[str], limit: int
    ) -> list[Candidate]:
        """Resolution blocking: same-type entities whose name or alias contains a token."""
        with self._span("find_candidates"), self.session(write=False) as session:
            return session.execute_read(
                _find_candidates, entity_type.value, list(tokens), limit
            )

    # --- reads (agent tool) ----------------------------------------------------
    @_connection_retry
    def run_read_query(
        self, cypher: str, parameters: dict[str, Any] | None = None
    ) -> ReadResult:
        """Run agent-authored Cypher in a READ transaction with timeout and row cap.

        READ access mode makes the server reject writes; pair it with a
        reader-role account (Enterprise) for least privilege.
        """
        max_rows = self._limits.max_rows

        @unit_of_work(timeout=self._limits.timeout_s)
        def work(tx: ManagedTransaction) -> ReadResult:
            result = tx.run(cast(LiteralString, cypher), parameters or {})
            rows: list[dict[str, Any]] = []
            for record in result:
                if len(rows) == max_rows:
                    return ReadResult(rows, truncated=True)
                rows.append(record.data())
            return ReadResult(rows, truncated=False)

        with self._span("agent_query", cypher), self.session(write=False) as session:
            return session.execute_read(work)

    def describe_schema(self) -> str:
        """Static data model plus the relation patterns actually present, with counts."""
        try:
            patterns = self.run_read_query(
                "MATCH (a:Entity)-[r]->(b:Entity) "
                "RETURN a.type AS source, type(r) AS relation, b.type AS target, count(*) AS n "
                "ORDER BY n DESC LIMIT 60"
            ).rows
        except (Neo4jError, DriverError):  # best effort; the agent can still explore
            return DATA_MODEL
        if not patterns:
            return DATA_MODEL
        lines = [
            f"(:{p['source']})-[:{p['relation']}]->(:{p['target']})  x{p['n']}"
            for p in patterns
        ]
        return (
            f"{DATA_MODEL}\n\nRelation patterns present (direction matters):\n"
            + "\n".join(lines)
        )


_FIND_CANDIDATES: LiteralString = """
MATCH (e:Entity) WHERE e.type = $type
  AND any(t IN $tokens WHERE toLower(e.name) CONTAINS t
          OR any(a IN coalesce(e.aliases, []) WHERE toLower(a) CONTAINS t))
RETURN e.key AS key, e.name AS name, coalesce(e.aliases, []) AS aliases,
       coalesce(e.description, '') AS description
LIMIT $limit
"""


def _find_candidates(
    tx: ManagedTransaction, entity_type: str, tokens: list[str], limit: int
) -> list[Candidate]:
    result = tx.run(_FIND_CANDIDATES, type=entity_type, tokens=tokens, limit=limit)
    return [Candidate(**record.data()) for record in result]


class WritePlan(NamedTuple):
    """The rows one replace_source writes: entities by label, mentions, relations by type."""

    entities: dict[str, list[dict[str, Any]]]
    mentions: list[dict[str, Any]]
    relations: dict[str, list[dict[str, Any]]]


def _plan_writes(triples: Sequence[GroundedTriple]) -> WritePlan:
    entities: dict[str, dict[str, Any]] = {}
    mentions: dict[tuple[str, str, str], dict[str, Any]] = {}
    relations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for grounded in triples:
        for resolved in (grounded.head, grounded.tail):
            ref = resolved.ref
            row = entities.setdefault(
                f"{resolved.key}\x1f{grounded.chunk_id}",
                {
                    "key": resolved.key,
                    "name": ref.name,  # only used when the entity is new
                    "type": ref.type.value,
                    "description": ref.description,
                    "chunk_id": grounded.chunk_id,
                    "source_id": grounded.source_id,
                    "codes": json.dumps(
                        {k.value: v for k, v in ref.proposed_codes.items()},
                        sort_keys=True,
                    ),
                },
            )
            row["description"] = row["description"] or ref.description
            for surface in {ref.name, *ref.aliases}:
                mentions.setdefault(
                    (resolved.key, grounded.chunk_id, surface),
                    {
                        "key": resolved.key,
                        "chunk_id": grounded.chunk_id,
                        "surface": surface,
                        "method": (
                            resolved.method.value
                            if surface == ref.name
                            else "stated_alias"
                        ),
                    },
                )
        # Defence in depth for the f-string below.
        if not RELATION_RE.fullmatch(grounded.relation):
            raise ValueError(f"unsafe relationship type {grounded.relation!r}")
        t = grounded.triple
        relations[grounded.relation].append(
            {
                "head_key": grounded.head.key,
                "tail_key": grounded.tail.key,
                "chunk_id": grounded.chunk_id,
                "evidence": t.evidence,
                "certainty": t.certainty.value,
                "valid_at": t.valid_at.isoformat() if t.valid_at else None,
                "predicate": grounded.original_relation or grounded.relation,
            }
        )
    by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in entities.values():
        by_label[row["type"]].append(row)
    return WritePlan(by_label, list(mentions.values()), relations)


def _write_lexical(
    tx: ManagedTransaction, document: DocumentMetadata, chunks: Sequence[ChunkRecord]
) -> None:
    tx.run(
        """
        MERGE (d:Document {source_id: $doc.source_id})
        SET d.title = $doc.title, d.doc_type = $doc.doc_type, d.source_uri = $doc.source_uri,
            d.effective_date = $doc.effective_date, d.sensitivity = $doc.sensitivity
        WITH d
        UNWIND $chunks AS row
        MERGE (c:Chunk {chunk_id: row.chunk_id})
        SET c.source_id = $doc.source_id, c.ordinal = row.ordinal,
            c.section_path = row.section_path, c.sensitivity = row.sensitivity
        MERGE (c)-[:PART_OF]->(d)
        """,
        doc=document.model_dump(mode="json"),
        chunks=[
            {
                "chunk_id": c.chunk_id,
                "ordinal": c.ordinal,
                "section_path": c.section_path,
                "sensitivity": c.sensitivity.value,
            }
            for c in chunks
        ],
    ).consume()
    ordered = sorted(chunks, key=lambda c: c.ordinal)
    tx.run(
        """
        MATCH (d:Document {source_id: $source_id}), (first:Chunk {chunk_id: $first})
        MERGE (d)-[:FIRST_CHUNK]->(first)
        WITH 1 AS done
        UNWIND $pairs AS pair
        MATCH (a:Chunk {chunk_id: pair[0]}), (b:Chunk {chunk_id: pair[1]})
        MERGE (a)-[:NEXT_CHUNK]->(b)
        """,
        source_id=document.source_id,
        first=ordered[0].chunk_id,
        pairs=[
            [a.chunk_id, b.chunk_id] for a, b in zip(ordered, ordered[1:], strict=False)
        ],
    ).consume()


def _write_domain(tx: ManagedTransaction, plan: WritePlan) -> None:
    for label, rows in plan.entities.items():
        # Labels cannot be parameters; only closed-enum literals reach here.
        EntityType(label)
        tx.run(
            cast(
                LiteralString,
                f"""
                UNWIND $rows AS row
                MERGE (e:Entity {{key: row.key}})
                  ON CREATE SET e.name = row.name, e.type = row.type, e.chunk_id = row.chunk_id,
                                e.chunk_ids = [], e.source_ids = [], e.aliases = [], e.description = row.description,
                                e.proposed_codes_json = row.codes, e.created_at = datetime()
                SET e:`{label}`,
                    e.chunk_ids = CASE WHEN row.chunk_id IN e.chunk_ids THEN e.chunk_ids ELSE e.chunk_ids + row.chunk_id END,
                    e.source_ids = CASE WHEN row.source_id IN e.source_ids THEN e.source_ids ELSE e.source_ids + row.source_id END,
                    e.description = CASE WHEN coalesce(e.description, '') = '' THEN row.description ELSE e.description END
                """,
            ),
            rows=rows,
        ).consume()
    tx.run(
        """
        UNWIND $rows AS row
        MATCH (e:Entity {key: row.key}), (c:Chunk {chunk_id: row.chunk_id})
        MERGE (e)-[m:MENTIONED_IN {surface: row.surface}]->(c)
        SET m.resolution = row.method
        """,
        rows=plan.mentions,
    ).consume()
    for rel_type, rows in plan.relations.items():
        # RELATED_TO keeps one edge per distinct out-of-ontology predicate.
        identity = "{predicate: row.predicate}" if rel_type == FALLBACK_RELATION else ""
        tx.run(
            cast(
                LiteralString,
                f"""
                UNWIND $rows AS row
                MATCH (h:Entity {{key: row.head_key}}), (t:Entity {{key: row.tail_key}})
                OPTIONAL MATCH (c:Chunk {{chunk_id: row.chunk_id}})
                MERGE (h)-[r:`{rel_type}` {identity}]->(t)
                  ON CREATE SET r.chunk_ids = [], r.extracted_as = [],
                                r.certainty = row.certainty, r.valid_at = row.valid_at,
                                r.created_at = datetime()
                // The quote keeps its own chunk's rank: sensitivity_rank falls as
                // sources are added, but the quote is still that chunk's text.
                WITH r, row, r.evidence_rank IS NULL AS quote, {_RANK} AS rank
                SET r.chunk_id = CASE WHEN quote THEN row.chunk_id ELSE r.chunk_id END,
                    r.evidence = CASE WHEN quote THEN row.evidence ELSE r.evidence END,
                    r.evidence_rank = CASE WHEN quote THEN rank ELSE r.evidence_rank END,
                    r.chunk_ids = CASE WHEN row.chunk_id IN r.chunk_ids THEN r.chunk_ids ELSE r.chunk_ids + row.chunk_id END,
                    r.extracted_as = CASE WHEN row.predicate IN r.extracted_as THEN r.extracted_as
                                          ELSE r.extracted_as + row.predicate END
                """,
            ),
            rows=rows,
        ).consume()


def _delete_source(
    tx: ManagedTransaction, source_id: str, keep: Sequence[str] = ()
) -> tuple[set[str], int]:
    """Remove a document's provenance and its chunks except `keep`.

    Returns the keys of entities it touched and how many chunks it deleted.
    """
    record = tx.run(
        """
        OPTIONAL MATCH (c:Chunk {source_id: $source_id})
        OPTIONAL MATCH (e:Entity)-[:MENTIONED_IN]->(c)
        RETURN collect(DISTINCT c.chunk_id) AS ids, collect(DISTINCT e.key) AS keys
        """,
        source_id=source_id,
    ).single(strict=True)
    stale, affected = record["ids"], set(record["keys"])
    statements: tuple[LiteralString, ...] = (
        # Remove the document's chunks from edge/entity provenance ...
        """MATCH ()-[r]->() WHERE any(id IN r.chunk_ids WHERE id IN $stale)
           SET r.chunk_ids = [id IN r.chunk_ids WHERE NOT id IN $stale]""",
        """MATCH (e:Entity) WHERE any(id IN e.chunk_ids WHERE id IN $stale)
           SET e.chunk_ids = [id IN e.chunk_ids WHERE NOT id IN $stale],
               e.source_ids = [s IN e.source_ids WHERE s <> $source_id]""",
        # ... drop whatever only this document supported ...
        "MATCH ()-[r]->() WHERE r.chunk_ids = [] DELETE r",
        # Kept chunks lose their mentions and order; the new version rewrites both.
        "MATCH (:Entity)-[m:MENTIONED_IN]->(:Chunk {source_id: $source_id}) DELETE m",
        "MATCH (:Chunk {source_id: $source_id})-[n:NEXT_CHUNK]->() DELETE n",
        "MATCH (c:Chunk {source_id: $source_id}) WHERE NOT c.chunk_id IN $keep DETACH DELETE c",
        "MATCH (d:Document {source_id: $source_id}) DETACH DELETE d",
        "MATCH (e:Entity) WHERE e.chunk_ids = [] DETACH DELETE e",
        # ... and re-point first-seen chunk_id where that chunk is gone. An edge's
        # quote goes with its chunk; the next chunk that states the edge re-quotes.
        "MATCH (e:Entity) WHERE NOT e.chunk_id IN e.chunk_ids SET e.chunk_id = e.chunk_ids[0]",
        """MATCH ()-[r]->() WHERE NOT r.chunk_id IN r.chunk_ids
           SET r.chunk_id = r.chunk_ids[0] REMOVE r.evidence, r.evidence_rank""",
    )
    for statement in statements:
        tx.run(statement, stale=stale, source_id=source_id, keep=list(keep)).consume()
    return affected, len(set(stale) - set(keep))


# A chunk's rank from its `sensitivity`; an unknown level counts as phi.
_PII, _PHI = Sensitivity.PII.rank, Sensitivity.PHI.rank
_RANK = (
    "CASE c.sensitivity "
    + "".join(f"WHEN '{level}' THEN {level.rank} " for level in Sensitivity)
    + f"ELSE {_PHI} END"
)


def _refresh_derived(tx: ManagedTransaction, keys: Sequence[str]) -> None:
    """Recompute aliases, sensitivity and labels of the given entities and their edges."""
    tx.run(
        cast(
            LiteralString,
            f"""
        UNWIND $keys AS key
        MATCH (e:Entity {{key: key}})
        WITH e, COLLECT {{ MATCH (e)-[:MENTIONED_IN]->(c:Chunk) RETURN DISTINCT {_RANK} }} AS ranks
        SET e.aliases = COLLECT {{
                MATCH (e)-[m:MENTIONED_IN]->(:Chunk) WHERE m.surface <> e.name
                RETURN DISTINCT m.surface AS surface ORDER BY surface
            }},
            e.sensitivity_rank = reduce(
                least = {_PHI}, r IN ranks | CASE WHEN r < least THEN r ELSE least END
            )
        // Sensitivity labels let Enterprise RBAC hide nodes per role,
        // e.g. DENY TRAVERSE ON GRAPH neo4j NODES PHI TO internal_reader.
        REMOVE e:PII:PHI
        WITH e
        FOREACH (_ IN CASE WHEN e.sensitivity_rank >= {_PII} THEN [1] ELSE [] END | SET e:PII)
        FOREACH (_ IN CASE WHEN e.sensitivity_rank >= {_PHI} THEN [1] ELSE [] END | SET e:PHI)
        """,
        ),
        keys=list(keys),
    ).consume()
    tx.run(
        cast(
            LiteralString,
            f"""
        UNWIND $keys AS key
        MATCH (:Entity {{key: key}})-[r]-(:Entity)
        WITH DISTINCT r
        WITH r, COLLECT {{
                MATCH (c:Chunk) WHERE c.chunk_id IN r.chunk_ids RETURN {_RANK}
            }} AS ranks
        SET r.sensitivity_rank = reduce(
                least = {_PHI}, x IN ranks | CASE WHEN x < least THEN x ELSE least END
            )
        """,
        ),
        keys=list(keys),
    ).consume()


_WRITE_KEYWORDS = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|FOREACH|LOAD\s+CSV|IN\s+TRANSACTIONS|"
    r"GRANT|REVOKE|DENY|ALTER|RENAME|TERMINATE|SHOW|USE)\b",
    re.IGNORECASE,
)
# Procedure calls are blocked except read-only introspection; CALL { } and
# CALL (vars) { } subqueries are allowed.
_PROCEDURE_CALL = re.compile(
    r"\bCALL\s+(?![({])(?!db\.(?:labels|relationshipTypes|propertyKeys)\s*\()",
    re.IGNORECASE,
)
# Chunk text lives on (:Chunk) nodes with CHUNK_STORE=neo4j; it is served only by
# the chunk tools, through the redactor. Reading it by name is rejected (computed
# keys still need database-side RBAC).
_CHUNK_TEXT_PROPERTY = re.compile(r"\.\s*`?(?:text|search_text|embedding)\b")
_CHUNK_TEXT_SUBSCRIPT = re.compile(r"\[\s*(['\"])(?:text|search_text|embedding)\1\s*\]")
_STRING_LITERAL = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)


class UnsafeCypherError(ValueError):
    """A query that is not a single read-only statement."""


def validate_read_only_cypher(cypher: str, *, max_chars: int = 4000) -> str:
    """Reject anything that is not a single read query, or that reads chunk text.

    Defence in depth only - the authoritative controls are the READ
    transaction and the database role.
    """
    query = cypher.strip().rstrip(";").strip()
    if not query:
        raise UnsafeCypherError("query is empty")
    if len(query) > max_chars:
        raise UnsafeCypherError(f"query exceeds {max_chars} characters")
    code = _STRING_LITERAL.sub("''", _COMMENT.sub(" ", query))
    if ";" in code:
        raise UnsafeCypherError("only one statement per call")
    if match := _WRITE_KEYWORDS.search(code):
        raise UnsafeCypherError(
            f"read-only access: {match.group(0).upper()} is not allowed"
        )
    if _PROCEDURE_CALL.search(code):
        raise UnsafeCypherError(
            "procedure calls are not allowed (subqueries and db.labels() etc. are)"
        )
    if _CHUNK_TEXT_PROPERTY.search(code) or _CHUNK_TEXT_SUBSCRIPT.search(query):
        raise UnsafeCypherError(
            "chunk text is not readable through Cypher; read it with the chunk tools"
        )
    return query
