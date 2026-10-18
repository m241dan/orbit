"""The envelopes commands and events travel in, and how the backend fills in events'."""

import asyncio

import pytest
from pydantic import ValidationError

from orbit.agent import AgentSession
from orbit.agent.domain.usage import UsageReport
from orbit.protocol import (
    DEFAULT_WORKSPACE,
    PLANNER,
    AddTask,
    CommandEnvelope,
    EventEnvelope,
    ListSessions,
    NewSession,
    Notice,
    SendMessage,
    SessionOpened,
    SessionsListed,
    TasksChanged,
    TaskSpec,
    UsageChanged,
)
from tests.agent_fakes import FakeStore, Ticks, greeted, send
from tests.test_usage_monitor import LIMITS, Source

HELLO = SendMessage(agent=PLANNER, text="hello")


def test_a_command_envelope_nests_its_command_under_payload_with_its_type() -> None:
    envelope = CommandEnvelope(command_id="c1", payload=HELLO)

    assert envelope.model_dump() == {
        "workspace": "default",
        "traceparent": None,
        "tracestate": None,
        "command_id": "c1",
        "session": None,
        "payload": {"type": "send_message", "agent": PLANNER, "text": "hello"},
    }


def test_a_command_envelope_reads_its_command_by_type() -> None:
    raw = {"command_id": "c1", "payload": {"type": "send_message", "agent": PLANNER, "text": "hi"}}

    assert CommandEnvelope.model_validate(raw).payload == SendMessage(agent=PLANNER, text="hi")


def test_a_command_envelope_needs_a_command_id() -> None:
    with pytest.raises(ValidationError, match="command_id"):
        CommandEnvelope(command_id="", payload=HELLO)


def test_an_event_envelope_counts_from_one() -> None:
    with pytest.raises(ValidationError, match="seq"):
        EventEnvelope(seq=0, payload=Notice(message="hi"))


@pytest.mark.parametrize(
    "raw",
    [
        {"command_id": "c1", "payload": HELLO, "extra": 1},
        {"seq": 1, "payload": Notice(message="hi"), "extra": 1},
    ],
)
def test_envelopes_refuse_unknown_fields(raw: dict[str, object]) -> None:
    kind = CommandEnvelope if "command_id" in raw else EventEnvelope
    with pytest.raises(ValidationError, match="extra"):
        kind.model_validate(raw)


def saved_session(usage: Source | None = None) -> AgentSession:
    """A session saved as ``session-1``, then ``session-2`` and so on, with usage from
    ``usage``."""
    ids = iter(f"session-{n}" for n in range(1, 100))
    return AgentSession(store=FakeStore(), clock=Ticks(), new_id=lambda: next(ids), usage=usage)


async def envelopes_until(session: AgentSession, kind: type) -> list[EventEnvelope]:
    """The session's envelopes up to and including the first holding a ``kind`` event, greeting
    it first if it hasn't been."""
    envelopes: list[EventEnvelope] = []
    await greeted(session)

    async def collect() -> None:
        async for envelope in session.events():
            envelopes.append(envelope)
            if isinstance(envelope.payload, kind):
                return

    await asyncio.wait_for(collect(), 5)
    return envelopes


async def test_events_are_numbered_from_one_across_a_session_switch() -> None:
    session = saved_session()
    before = await envelopes_until(session, SessionOpened)
    await send(session, NewSession(name="other"))
    after = await envelopes_until(session, SessionOpened)
    await session.close()

    seqs = [envelope.seq for envelope in before + after]
    assert seqs == list(range(1, len(seqs) + 1))
    assert before[-1].session == "session-1"
    assert after[-1].session == "session-2"


async def test_events_carry_the_open_session_unless_they_belong_to_none() -> None:
    session = saved_session()
    await envelopes_until(session, SessionOpened)
    await send(session, AddTask(task=TaskSpec(prompt="Add a flag.")))
    tasks = await envelopes_until(session, TasksChanged)
    await send(session, ListSessions())
    listed = await envelopes_until(session, SessionsListed)
    await session.close()

    assert tasks[-1].session == "session-1"
    assert listed[-1].session is None
    assert {e.workspace for e in tasks + listed} == {DEFAULT_WORKSPACE}


async def test_usage_belongs_to_no_session() -> None:
    session = saved_session(Source(UsageReport(LIMITS)))
    [*_, usage] = await envelopes_until(session, UsageChanged)
    await session.close()

    assert usage.session is None
    assert usage.workspace == DEFAULT_WORKSPACE


async def test_an_event_keeps_the_session_it_was_published_in_when_read_after_a_switch() -> None:
    session = saved_session()
    await send(session, AddTask(task=TaskSpec(prompt="Add a flag.")))
    await send(session, NewSession(name="other"))
    await envelopes_until(session, SessionOpened)  # Session-1's, announced at the Welcome.
    after = await envelopes_until(session, SessionOpened)
    await session.close()

    tasks = [e for e in after if isinstance(e.payload, TasksChanged)]
    assert tasks[0].session == "session-1"
    assert after[-1].session == "session-2"
