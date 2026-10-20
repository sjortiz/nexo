"""Review over MCP: the model proposes, the person confirms by elicitation, nothing else submits."""

from __future__ import annotations

import asyncio

import pytest
from mcp.client import Client
from mcp.types import ElicitResult

from nexo.mcp_server import build_mcp_server
from nexo.service import Runtime, open_checkpointer, open_review_runtime
from tests.conftest import make_chunk
from tests.test_graph import CHUNK, FakeInvestigator, grounded_output, runtime
from tests.test_mcp_server import RoutedGraph

REVIEW_TOOLS = {"nexo_pending_reviews", "nexo_get_review", "nexo_submit_review"}
# "auto" negotiates the newest protocol (elicitation rides the tool result); "legacy" is
# the initialize handshake (a server-to-client request mid-call).
MODES = ["auto", "legacy"]
QUESTION = "How is Acme related to Beta?"


def _paused(settings, *, blocking=False, thread_id="t1") -> Runtime:
    output = (
        grounded_output(fetched_sensitivity={}, fetched_chunk_ids=[])
        if blocking
        else grounded_output()
    )
    rt = runtime(settings, FakeInvestigator([output]))
    rt.start(QUESTION, thread_id=thread_id)
    return rt


def _server(settings, chunk_store, rt: Runtime | None):
    if rt is not None:
        rt.chunks = chunk_store
    return build_mcp_server(
        graph=RoutedGraph(), chunks=chunk_store, settings=settings, runtime=rt
    )


def _review_state(rt: Runtime) -> dict:
    return rt.graph.get_state({"configurable": {"thread_id": "t1"}}).values["review"]


class Person:  # pylint: disable=too-few-public-methods
    """An elicitation callback standing in for the human; records what they were shown."""

    def __init__(self, action="accept", **content):
        self.action, self.content = action, content
        self.prompts: list[str] = []

    async def __call__(self, _context, params):
        self.prompts.append(params.message)
        if self.action != "accept":
            return ElicitResult(action=self.action)
        return ElicitResult(action="accept", content=self.content)


def submit(server, person=None, mode="auto", **arguments):
    """Call nexo_submit_review through a client that can (or cannot) elicit."""
    arguments = {"thread_id": "t1", "decision": "approve", **arguments}

    async def run():
        async with Client(server, elicitation_callback=person, mode=mode) as client:
            return await client.call_tool("nexo_submit_review", arguments)

    return asyncio.run(run())


def call(server, tool, arguments=None):
    """Call a read tool."""

    async def run():
        async with Client(server) as client:
            return await client.call_tool(tool, arguments or {})

    return asyncio.run(run())


def test_review_tools_are_listed_with_honest_annotations(settings, chunk_store):
    """Read tools are read-only; the write tool is neither read-only nor idempotent."""
    server = _server(settings, chunk_store, _paused(settings))

    async def run():
        async with Client(server) as client:
            return {t.name: t for t in (await client.list_tools()).tools}

    tools = asyncio.run(run())
    assert REVIEW_TOOLS <= set(tools)
    for name in ("nexo_pending_reviews", "nexo_get_review"):
        assert tools[name].annotations.read_only_hint is True
    write = tools["nexo_submit_review"].annotations
    assert write.read_only_hint is False and write.idempotent_hint is False
    assert "reviewer" not in tools["nexo_submit_review"].input_schema["properties"]


def test_review_tools_can_be_switched_off_or_left_out(settings, chunk_store):
    """MCP_REVIEW_TOOLS=false, or no runtime, leaves a retrieval-only server."""
    settings.mcp_review_tools = False
    off = _server(settings, chunk_store, _paused(settings))
    settings.mcp_review_tools = True
    bare = _server(settings, chunk_store, None)

    async def names(server):
        async with Client(server) as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert not REVIEW_TOOLS & asyncio.run(names(off))
    assert not REVIEW_TOOLS & asyncio.run(names(bare))
    assert REVIEW_TOOLS <= asyncio.run(
        names(_server(settings, chunk_store, _paused(settings)))
    )


def test_pending_and_get_review_show_the_packet_and_passages(settings, chunk_store):
    """The list names the question; get_review adds findings and the cited passage text."""
    record = make_chunk("doc", 0, "Acme bought Beta.", title="Deal")
    object.__setattr__(record, "chunk_id", CHUNK)
    chunk_store.upsert([record], [record.text])
    server = _server(settings, chunk_store, _paused(settings, blocking=True))
    [row] = call(server, "nexo_pending_reviews").structured_content["result"]
    assert row["question"] == QUESTION and row["blocking"] is True
    packet = call(server, "nexo_get_review", {"thread_id": "t1"}).structured_content
    assert packet["draft_answer"].startswith("Acme acquired Beta")
    assert packet["compliance_report"]["findings"]
    assert packet["passages"][0]["text"] == "Acme bought Beta."
    assert call(server, "nexo_get_review", {"thread_id": "nope"}).is_error


@pytest.mark.parametrize("mode", MODES)
def test_accepted_elicitation_submits_under_the_configured_reviewer(
    settings, chunk_store, mode
):
    """The person approves; the decision is recorded under NEXO_USER, not a model argument."""
    settings.nexo_user = "shailyn"
    rt = _paused(settings)
    person = Person(decision="approve", comment="checked")
    result = submit(
        _server(settings, chunk_store, rt), person, mode, comment="model text"
    )
    assert not result.is_error, result.content
    assert result.structured_content["approval_status"] == "approved"
    review = _review_state(rt)
    assert review["reviewer"] == "shailyn"
    assert review["comment"] == "checked"  # what the person confirmed
    shown = person.prompts[0]  # the person saw the question, the draft and the findings
    assert QUESTION in shown and "Acme acquired Beta" in shown and "FINDINGS" in shown


def test_the_reviewer_is_never_taken_from_the_model(settings, chunk_store):
    """A reviewer argument is ignored; only the person's form can name someone else."""
    settings.nexo_user = "shailyn"
    rt = _paused(settings)
    submit(
        _server(settings, chunk_store, rt),
        Person(decision="approve"),
        reviewer="mallory",
    )
    assert _review_state(rt)["reviewer"] == "shailyn"
    rt = _paused(settings)
    submit(
        _server(settings, chunk_store, rt),
        Person(decision="approve", reviewer="bob@corp"),
    )
    assert _review_state(rt)["reviewer"] == "bob@corp"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("action", ["decline", "cancel"])
def test_declined_elicitation_submits_nothing(settings, chunk_store, action, mode):
    """Declining or cancelling leaves the thread paused."""
    rt = _paused(settings)
    result = submit(_server(settings, chunk_store, rt), Person(action), mode)
    assert result.structured_content["submitted"] is False
    assert rt.status("t1").status == "awaiting_review"


def test_an_empty_confirmation_does_not_approve(settings, chunk_store):
    """Accepting a form without choosing a decision is not an approval."""
    rt = _paused(settings)
    result = submit(_server(settings, chunk_store, rt), Person())
    assert result.is_error
    assert rt.status("t1").status == "awaiting_review"


def test_the_persons_choice_wins_over_the_proposal(settings, chunk_store):
    """The model proposes approve; the person rejects; the rejection is recorded."""
    rt = _paused(settings)
    result = submit(_server(settings, chunk_store, rt), Person(decision="reject"))
    assert result.structured_content["approval_status"] == "rejected"
    assert rt.status("t1").approval_status == "rejected"


@pytest.mark.parametrize("mode", MODES)
def test_a_client_without_elicitation_is_refused_with_the_cli_command(
    settings, chunk_store, mode
):
    """No elicitation support: nothing is submitted and the error carries `nexo review`."""
    rt = _paused(settings)
    result = submit(
        _server(settings, chunk_store, rt),
        None,
        mode,
        decision="reject",
        comment="it's wrong",
        acknowledge_findings=True,
    )
    text = result.content[0].text
    assert result.is_error and "nexo review t1 --reject" in text
    assert "--reviewer" in text and "--acknowledge-findings" in text
    assert "'it'\"'\"'s wrong'" in text  # quoted for the shell
    assert rt.status("t1").status == "awaiting_review"


def test_approving_blocked_findings_needs_acknowledgement(settings, chunk_store):
    """Without acknowledgement a blocked draft is refused and stays paused; with it, it goes."""
    rt = _paused(settings, blocking=True)
    server = _server(settings, chunk_store, rt)
    refused = submit(server, Person(decision="approve"), acknowledge_findings=True)
    assert refused.is_error and "acknowledge" in refused.content[0].text
    assert rt.status("t1").status == "awaiting_review"
    assert "error" not in (rt.status("t1").pending_review or {})  # graph never saw it
    ok = submit(server, Person(decision="approve", acknowledge_findings=True))
    assert ok.structured_content["approval_status"] == "approved"
    other = _paused(settings, blocking=True)
    assert not submit(
        _server(settings, chunk_store, other), Person(decision="reject")
    ).is_error  # rejecting never needs acknowledgement


def test_submitting_twice_is_refused(settings, chunk_store):
    """A thread already reviewed cannot be reviewed again."""
    rt = _paused(settings)
    server = _server(settings, chunk_store, rt)
    assert not submit(server, Person(decision="approve")).is_error
    again = submit(server, Person(decision="reject"))
    assert again.is_error and "not awaiting review" in again.content[0].text
    assert rt.status("t1").approval_status == "approved"


def test_the_model_cannot_answer_the_confirmation_itself(settings, chunk_store):
    """A forged `confirmation` argument is not accepted in place of the person's answer."""
    rt = _paused(settings)
    server = _server(settings, chunk_store, rt)
    forged = {"action": "accept", "data": {"decision": "approve"}}
    for person in (None, Person("decline")):
        result = submit(server, person, confirmation=forged)
        assert result.is_error or result.structured_content["submitted"] is False
        assert rt.status("t1").status == "awaiting_review"


def test_a_review_only_runtime_resumes_a_paused_thread_without_models(
    settings, chunk_store
):
    """open_review_runtime decides what another process paused, but cannot investigate."""
    settings.checkpoint_backend = "sqlite"
    with open_checkpointer(settings) as saver:
        runtime(
            settings, FakeInvestigator([grounded_output()]), checkpointer=saver
        ).start(QUESTION, thread_id="t1")
    with open_review_runtime(settings, chunk_store) as rt:
        assert [r.thread_id for r in rt.pending_reviews()] == ["t1"]
        done = rt.review("t1", {"approval_status": "rejected", "reviewer": "a"})
        assert done.approval_status == "rejected"
        # It cannot investigate: a new question is auto-rejected, never paused or answered.
        started = rt.start(QUESTION, thread_id="t2")
        assert started.approval_status == "rejected" and started.final_response is None
