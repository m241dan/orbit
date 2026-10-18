"""Catching a reconnecting frontend up on the events it missed: when it can, and what it gets."""

import pytest

from orbit.agent import AgentSession
from orbit.agent.domain.replay import LogBounds, can_replay
from orbit.protocol import (
    PROTOCOL_VERSION,
    Catalog,
    CommandEnvelope,
    EventEnvelope,
    Hello,
    Notice,
    Snapshot,
    Welcome,
)
from tests.agent_fakes import HELLO_ID, FakeStore, Ticks
from tests.test_log_writer import GatedLog, delivered

OPEN = "session-1"

# The rule


@pytest.mark.parametrize(
    ("since", "bounds"),
    [
        (15, LogBounds(first_seq=10, others_seq=0)),
        (9, LogBounds(first_seq=10, others_seq=0)),
        (20, LogBounds(first_seq=10, others_seq=0)),
        (15, LogBounds(first_seq=10, others_seq=15)),
        (15, LogBounds(first_seq=None, others_seq=3)),
    ],
)
def test_a_frontend_catches_up_on_the_open_sessions_events_alone(
    since: int, bounds: LogBounds
) -> None:
    assert can_replay(since, 20, bounds)


@pytest.mark.parametrize(
    ("since", "bounds"),
    [
        (15, LogBounds(first_seq=10, others_seq=18)),  # Another session's came after it.
        (8, LogBounds(first_seq=10, others_seq=0)),  # Older than what's kept.
        (21, LogBounds(first_seq=10, others_seq=0)),  # Ahead of the last stamped.
    ],
)
def test_a_frontend_cant_catch_up_otherwise(since: int, bounds: LogBounds) -> None:
    assert not can_replay(since, 20, bounds)


# What the session sends


def notice(seq: int, session: str = OPEN) -> EventEnvelope:
    return EventEnvelope(seq=seq, session=session, payload=Notice(message=f"n{seq}"))


def notices(first: int, end: int) -> list[EventEnvelope]:
    return [notice(seq) for seq in range(first, end)]


def store_with(*logged: EventEnvelope) -> FakeStore:
    store = FakeStore()
    store.logged = list(logged)
    return store


async def reconnected(store: FakeStore | None, since: int | None) -> list[EventEnvelope]:
    """What a session over ``store`` sends a frontend greeting it with ``since``."""
    session = AgentSession(store=store, clock=Ticks(), new_id=lambda: OPEN, workspace="/work")
    hello = Hello(client="orbit-tests", protocol_version=PROTOCOL_VERSION, since=since)
    await session.send(CommandEnvelope(command_id=HELLO_ID, payload=hello))
    await session.close()
    return delivered(session)


def kinds(sent: list[EventEnvelope]) -> list[type]:
    return [type(envelope.payload) for envelope in sent]


async def test_a_reconnecting_frontend_gets_what_it_missed_in_place_of_a_snapshot() -> None:
    store = store_with(*(notice(seq) for seq in range(10, 21)))
    sent = await reconnected(store, 15)

    assert kinds(sent[:2]) == [Welcome, Catalog]
    assert sent[2:7] == notices(16, 21)
    assert sent[7].payload == Notice(message="Workspace: /work")
    assert Snapshot not in kinds(sent)
    assert [e for _, appended in store.appends for e in appended] == sent[7:]  # Logged once.


async def test_events_still_to_be_written_are_caught_up_on_once() -> None:
    store = GatedLog()
    session = AgentSession(store=store, clock=Ticks(), new_id=lambda: OPEN)
    await session._events.put(Notice(message="a"))
    await store.waiting.wait()  # 1 is being written.
    await session._events.put(Notice(message="b"))
    delivered(session)  # Sent before the frontend went away.
    hello = Hello(client="orbit-tests", protocol_version=PROTOCOL_VERSION, since=0)
    await session.send(CommandEnvelope(command_id=HELLO_ID, payload=hello))
    store.gate.set()
    await session.close()

    replayed = [e for e in delivered(session) if isinstance(e.payload, Notice)]
    assert [(e.seq, e.payload) for e in replayed] == [
        (1, Notice(message="a")),
        (2, Notice(message="b")),
    ]


@pytest.mark.parametrize(
    "logged",
    [
        [notice(seq) for seq in range(10, 18)] + [notice(18, "other")],
        [notice(seq) for seq in range(17, 21)],
    ],
    ids=["another session came after it", "older than what's kept"],
)
async def test_a_frontend_that_cant_catch_up_gets_a_snapshot(logged: list[EventEnvelope]) -> None:
    sent = await reconnected(store_with(*logged), 15)

    assert kinds(sent[:3]) == [Welcome, Catalog, Snapshot]
    assert not [envelope for envelope in sent if envelope.payload == Notice(message="n16")]


async def test_a_frontend_ahead_of_the_log_gets_a_snapshot() -> None:
    sent = await reconnected(store_with(*(notice(seq) for seq in range(10, 21))), 21)
    assert kinds(sent[:3]) == [Welcome, Catalog, Snapshot]


async def test_a_frontend_with_no_store_to_catch_up_from_gets_a_snapshot() -> None:
    sent = await reconnected(None, 0)
    assert kinds(sent[:3]) == [Welcome, Catalog, Snapshot]


async def test_a_frontend_that_gave_no_seq_gets_a_snapshot() -> None:
    sent = await reconnected(store_with(*(notice(seq) for seq in range(10, 21))), None)
    assert kinds(sent[:3]) == [Welcome, Catalog, Snapshot]


class Unreadable(FakeStore):
    """A log whose bounds can be read, but not its events."""

    async def after(self, session_id: str, after_seq: int) -> list[EventEnvelope] | None:
        return None


async def test_a_frontend_whose_missed_events_cant_be_read_gets_a_snapshot() -> None:
    sent = await reconnected(Unreadable(), 0)
    assert kinds(sent[:3]) == [Welcome, Catalog, Snapshot]
