"""`nexo delete`: operators on the allowlist remove a source; everyone else is refused."""

from __future__ import annotations

import json
import logging

import pytest

from nexo import cli, service
from nexo.config import Settings
from nexo.storage.graph_store import SourceRemoval


class _Graph:
    """Stands in for GraphStore: records the removal and whether it was closed."""

    def __init__(self) -> None:
        self.removed: list[str] = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.closed = True

    def remove_source(self, source_id: str) -> SourceRemoval:
        """Record the removal and report fixed counts."""
        self.removed.append(source_id)
        return SourceRemoval(chunks_removed=3, entities_touched=2)


class _Chunks:  # pylint: disable=too-few-public-methods
    """A chunk store that counts the chunks a deletion took out."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_source(self, source_id: str, keep=frozenset()) -> int:
        """Record the deletion and report three chunks."""
        assert not keep
        self.deleted.append(source_id)
        return 3


@pytest.fixture(name="stores")
def fixture_stores(monkeypatch):
    """Fake graph and chunk store behind the service's factories."""
    graph, chunks = _Graph(), _Chunks()
    monkeypatch.setattr(
        service.GraphStore,
        "from_settings",
        classmethod(lambda cls, s, read_only: graph),
    )
    monkeypatch.setattr(service, "chunk_store_from_settings", lambda s, g: chunks)
    return graph, chunks


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None).model_copy(update=overrides)


def test_an_operator_on_the_allowlist_removes_the_source_and_leaves_an_audit_record(
    stores, caplog
):
    """Graph and chunk store are both asked; the audit record names who and how much."""
    graph, chunks = stores
    settings = _settings(nexo_user="ana", delete_operators="ana, bob")
    with caplog.at_level(logging.INFO, logger="nexo.audit"):
        record = service.remove_source(settings, "memo-7")
    assert record == {
        "source_id": "memo-7",
        "operator": "ana",
        "chunks_removed": 3,
        "entities_touched": 2,
    }
    assert graph.removed == ["memo-7"] and chunks.deleted == ["memo-7"] and graph.closed
    assert any(
        r.name == "nexo.audit" and "delete memo-7 by ana: 3 chunks removed" in r.message
        for r in caplog.records
    )


def test_anyone_else_is_refused_before_anything_is_touched(stores):
    """Not on the allowlist (or an empty allowlist): PermissionError, no store call."""
    graph, chunks = stores
    with pytest.raises(PermissionError, match="carl is not a delete operator"):
        service.remove_source(_settings(nexo_user="carl", delete_operators="ana"), "m")
    with pytest.raises(PermissionError):
        service.remove_source(_settings(nexo_user="ana", delete_operators=""), "m")
    assert not graph.removed and not chunks.deleted


def test_the_cli_prints_the_record_or_exits_2(
    stores, monkeypatch, capsys
):  # pylint: disable=unused-argument
    """`nexo delete` emits the record as JSON; a refusal is an error on stderr, exit 2."""
    allowed = _settings(nexo_user="ana", delete_operators="ana")
    monkeypatch.setattr(cli, "get_settings", lambda: allowed)
    try:
        assert cli.main(["delete", "memo-7"]) == 0
        out, err = capsys.readouterr()
        assert json.loads(out)["chunks_removed"] == 3 and "deleted memo-7" in err
        monkeypatch.setattr(cli, "get_settings", lambda: _settings(nexo_user="carl"))
        assert cli.main(["delete", "memo-7"]) == 2
        _, err = capsys.readouterr()
        assert "carl is not a delete operator" in err
    finally:
        logging.getLogger().handlers.clear()  # main bound a handler to captured stderr
