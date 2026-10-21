"""One paused packet as every review surface shows it, against a stored snapshot.

The snapshot pins what reviewers see. When a change is meant to alter it, rewrite
the file with UPDATE_SNAPSHOTS=1 and review the diff.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from langgraph.types import Command

from nexo.cli import _emit
from nexo.review_inbox import InboxAdapter
from nexo.review_ui import ReviewApp
from tests.conftest import make_chunk
from tests.test_graph import CHUNK, FakeInvestigator, grounded_output, runtime
from tests.test_mcp_review import QUESTION, _server, call

SNAPSHOT = Path(__file__).parent / "snapshots" / "review_packet.json"


def _without_paused_at(out: str) -> dict[str, Any]:
    """The CLI's JSON without the pause time, which changes on every run."""
    result = json.loads(out)
    assert result.pop("paused_at")
    return result


def _surfaces(settings, chunk_store, capsys) -> dict[str, Any]:
    record = make_chunk("doc", 0, "Acme bought Beta.", title="Deal")
    object.__setattr__(record, "chunk_id", CHUNK)
    chunk_store.upsert([record], [record.text])
    # A citation the agent never read: a blocking finding.
    output = grounded_output(fetched_sensitivity={}, fetched_chunk_ids=[])
    rt = runtime(settings, FakeInvestigator([output]))
    rt.chunks = chunk_store
    rt.start(QUESTION, thread_id="t1")
    settings.nexo_user = "ana"
    config = {"configurable": {"thread_id": "t1"}}
    [task] = rt.graph.get_state(config).tasks
    seen: dict[str, Any] = {"checkpoint": task.interrupts[-1].value}

    _emit(rt.status("t1"), "status")
    seen["cli"] = _without_paused_at(capsys.readouterr().out)

    server = _server(settings, chunk_store, rt)
    seen["mcp_pending"] = call(server, "nexo_pending_reviews").structured_content
    seen["mcp_get"] = call(
        server, "nexo_get_review", {"thread_id": "t1"}
    ).structured_content

    app = ReviewApp(rt, settings)
    page = app.handle("GET", "/review/t1")[1]
    seen["review_ui"] = page.replace(app.csrf_token, "<csrf>")

    inbox = InboxAdapter(rt, reviewer="ana", clearance="internal")
    seen["inbox"] = inbox.get_state("t1")["tasks"][0]["interrupts"][0]["value"]

    # A malformed raw resume re-pauses with an error in the packet.
    rt.graph.invoke(Command(resume={"approval_status": "maybe"}), config)
    _emit(rt.status("t1"), "status")
    seen["cli_error"] = _without_paused_at(capsys.readouterr().out)
    seen["mcp_get_error"] = call(
        server, "nexo_get_review", {"thread_id": "t1"}
    ).structured_content
    return seen


def test_every_surface_shows_the_packet_as_before(settings, chunk_store, capsys):
    """CLI, MCP, the demo page and Agent Inbox render one packet as the snapshot."""
    seen = _surfaces(settings, chunk_store, capsys)
    if os.environ.get("UPDATE_SNAPSHOTS"):
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(json.dumps(seen, indent=2) + "\n")
    expected = json.loads(SNAPSHOT.read_text())
    for surface, shown in seen.items():
        # Compared as text, so key order counts too.
        assert json.dumps(shown) == json.dumps(expected[surface]), surface
