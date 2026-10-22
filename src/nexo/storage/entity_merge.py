"""`nexo entities duplicates|merge`: one node for a name typed several ways.

Resolution never merges entities of different types, so a name the extractor
typed inconsistently (a team as Organization in one chunk, Concept in another)
becomes several nodes. `duplicates` lists them; `merge` moves every mention and
relation of the given entities onto one target, drops the rest and recomputes
the target's aliases and ranks. Recording the merge in CANONICAL_NAMES_PATH
keeps it after re-ingestion, which would otherwise rebuild the split.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, LiteralString, cast

from nexo.storage.graph_store import _refresh_derived
from nexo.storage.schemas import RELATION_RE

_LEXICAL = ["MENTIONED_IN", "PART_OF", "NEXT_CHUNK", "FIRST_CHUNK"]

# Same name after the type prefix, different key (so different type).
_DUPLICATES = """
MATCH (e:Entity)
WITH substring(e.key, size(split(e.key, ':')[0]) + 1) AS name, e
WITH name, collect(e) AS group WHERE size(group) > 1
UNWIND group AS e
WITH name, e, COUNT { (e)-[:MENTIONED_IN]->() } AS mentions,
     COUNT { (e)-[r]-(:Entity) WHERE NOT type(r) IN $lexical } AS relations
ORDER BY name, mentions DESC
RETURN name, collect({key: e.key, name: e.name, type: e.type, mentions: mentions,
                      relations: relations, documents: size(e.source_ids)}) AS entities
ORDER BY size(entities) DESC, name
"""


def find_duplicates(session: Any) -> list[dict[str, Any]]:
    """Groups of entities that share a normalized name but not a type."""
    return session.run(_DUPLICATES, lexical=_LEXICAL).data()


def merge_entities(tx: Any, target: str, sources: Sequence[str]) -> dict[str, Any]:
    """Move every mention and relation of `sources` onto `target`, then delete them."""
    keys = [s for s in dict.fromkeys(sources) if s != target]
    found = tx.run(
        "MATCH (e:Entity) WHERE e.key IN $keys RETURN e.key AS key, e.name AS name",
        keys=[target, *keys],
    ).data()
    names = {r["key"]: r["name"] for r in found}
    missing = [k for k in [target, *keys] if k not in names]
    if missing:
        raise ValueError(f"no entity with key {missing}")
    moved = {"mentions": 0, "relations": 0}
    for source in keys:
        moved["mentions"] += _move_mentions(tx, source, target)
        moved["relations"] += _move_relations(tx, source, target)
        tx.run(
            """
            MATCH (s:Entity {key: $source}), (t:Entity {key: $target})
            SET t.chunk_ids = t.chunk_ids + [c IN s.chunk_ids WHERE NOT c IN t.chunk_ids],
                t.source_ids = t.source_ids + [d IN s.source_ids WHERE NOT d IN t.source_ids]
            DETACH DELETE s
            """,
            source=source,
            target=target,
        ).consume()
    _refresh_derived(tx, [target])
    return {
        "target": target,
        "merged": keys,
        "names": [names[k] for k in keys],
        "target_name": names[target],
        **moved,
    }


def _move_mentions(tx: Any, source: str, target: str) -> int:
    record = tx.run(
        """
        MATCH (s:Entity {key: $source})-[m:MENTIONED_IN]->(c:Chunk), (t:Entity {key: $target})
        MERGE (t)-[n:MENTIONED_IN {surface: m.surface}]->(c)
          ON CREATE SET n.resolution = 'merged'
        SET n.attributes_json = coalesce(n.attributes_json, m.attributes_json)
        DELETE m
        RETURN count(*) AS moved
        """,
        source=source,
        target=target,
    ).single()
    return int(record["moved"]) if record else 0


def _move_relations(tx: Any, source: str, target: str) -> int:
    types = tx.run(
        "MATCH (:Entity {key: $source})-[r]-(:Entity) WHERE NOT type(r) IN $lexical "
        "RETURN DISTINCT type(r) AS t",
        source=source,
        lexical=_LEXICAL,
    ).data()
    moved = 0
    for row in types:
        rel = row["t"]
        if not RELATION_RE.fullmatch(rel):  # labels are spliced into Cypher below
            raise ValueError(f"unsafe relationship type {rel!r}")
        identity = "{predicate: r.predicate}" if rel == "RELATED_TO" else ""
        for pattern in (
            f"(s)-[r:`{rel}`]->(o:Entity)",  # outgoing: target -> o
            f"(o:Entity)-[r:`{rel}`]->(s)",  # incoming: o -> target
        ):
            outgoing = pattern.startswith("(s)")
            edge = (
                f"(t)-[n:`{rel}` {identity}]->(o)"
                if outgoing
                else f"(o)-[n:`{rel}` {identity}]->(t)"
            )
            record = tx.run(
                cast(
                    LiteralString,
                    f"""
                    MATCH (s:Entity {{key: $source}}), (t:Entity {{key: $target}})
                    MATCH {pattern}
                    WITH s, t, r, o WHERE o.key <> $target
                    MERGE {edge}
                      ON CREATE SET n = properties(r)
                      ON MATCH SET n.chunk_ids = n.chunk_ids
                                 + [c IN r.chunk_ids WHERE NOT c IN n.chunk_ids]
                    DELETE r
                    RETURN count(*) AS moved
                    """,
                ),
                source=source,
                target=target,
            ).single()
            moved += int(record["moved"]) if record else 0
    return moved
