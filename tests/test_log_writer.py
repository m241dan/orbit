"""The writer keeping sessions' events in the store's log, and what the session logs through it."""

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from orbit.agent import AgentSession
from orbit.agent.application.log_writer import LogWriter
from orbit.agent.external.sqlite_store import SqliteStore
from orbit.protocol import (
    SAVE_FAILED,
    AddTask,
    Catalog,
    CommandAccepted,
    ErrorOccurred,
    Event,
    EventEnvelope,
    Notice,
    SessionsListed,
    Snapshot,
    TasksChanged,
    TaskSpec,
    UsageChanged,
    Welcome,
    decode_event,
)
from tests.agent_fakes import FakeStore, Ticks, greet, publish, send
from tests.test_sqlite_store import database


def notice(seq: int, session: str = "a") -> EventEnvelope:
    return EventEnvelope(seq=seq, session=session, payload=Notice(message=f"n{seq}"))


class Published:
    """The events a writer reported, in a list."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)


class GatedLog(FakeStore):
    """A log whose appends wait for ``gate``, with ``waiting`` set while one does."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.waiting = asyncio.Event()

    async def append(self, session_id: str, envelopes: Sequence[EventEnvelope]) -> None:
        self.waiting.set()
        await self.gate.wait()
        await super().append(session_id, envelopes)


def stored_session(store: FakeStore | SqliteStore) -> AgentSession:
    return AgentSession(store=store, clock=Ticks(), new_id=lambda: "session-1")


def delivered(session: AgentSession) -> list[EventEnvelope]:
    """The envelopes the session has queued for the frontend, taken from its queue."""
    queue = session._events._queue
    return [queue.get_nowait() for _ in range(queue.qsize())]


def kept(sent: Sequence[EventEnvelope]) -> list[EventEnvelope]:
    """The envelopes of ``sent`` the log keeps: a session's, but for those answering one
    connection."""
    connection = Welcome | Catalog | Snapshot
    return [e for e in sent if e.session is not None and not isinstance(e.payload, connection)]


def payloads_of[T](kind: type[T], sent: Sequence[EventEnvelope]) -> list[T]:
    """The payloads of ``sent`` that are ``kind``s, in order."""
    return [envelope.payload for envelope in sent if isinstance(envelope.payload, kind)]


def read_log(state: Path) -> list[EventEnvelope | None]:
    """The events in the database in ``state``, decoded, in ``seq`` order."""
    with database(state) as connection:
        rows = connection.execute("SELECT json FROM events ORDER BY seq").fetchall()
    return [decode_event(text) for (text,) in rows]


# The writer


async def test_what_queued_is_written_in_one_append_per_session_in_order() -> None:
    log = FakeStore()
    writer = LogWriter(log, Published())
    for session, seq in [("a", 1), ("b", 2), ("a", 3)]:
        writer.add(session, notice(seq, session))
    await writer.close()

    assert log.appends == [("a", [notice(1), notice(3)]), ("b", [notice(2, "b")])]


async def test_events_added_while_an_append_runs_are_written_after_it() -> None:
    log = GatedLog()
    writer = LogWriter(log, Published())
    writer.add("a", notice(1))
    await log.waiting.wait()
    writer.add("a", notice(2))
    closing = asyncio.create_task(writer.close())
    log.gate.set()
    await closing

    assert log.appends == [("a", [notice(1)]), ("a", [notice(2)])]


async def test_a_failing_log_is_reported_once_and_later_events_are_still_written() -> None:
    log, published = FakeStore(), Published()
    log.append_failure = "disk full"
    writer = LogWriter(log, published)
    writer.add("a", notice(1))
    await asyncio.sleep(0.01)
    writer.add("a", notice(2))
    await asyncio.sleep(0.01)
    log.append_failure = None
    writer.add("a", notice(3))
    await writer.close()

    assert published.events == [ErrorOccurred(code=SAVE_FAILED, message="disk full")]
    assert log.logged == [notice(3)]


async def test_a_writer_closed_writes_nothing_more() -> None:
    log = FakeStore()
    writer = LogWriter(log, Published())
    await writer.close()
    writer.add("a", notice(1))
    await writer.close()
    await asyncio.sleep(0.01)

    assert log.appends == []


async def test_a_cancelled_close_stops_the_writer_at_once() -> None:
    log = GatedLog()
    writer = LogWriter(log, Published())
    writer.add("a", notice(1))
    await log.waiting.wait()
    closing = asyncio.create_task(writer.close())
    await asyncio.sleep(0)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    writer.add("a", notice(2))
    log.gate.set()
    await asyncio.sleep(0.01)

    assert log.appends == []
    assert asyncio.all_tasks() == {asyncio.current_task()}


# What the session logs


@pytest.fixture
async def sqlite(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    workspace = tmp_path / "project"
    workspace.mkdir()
    store = await SqliteStore.open(tmp_path / "state", str(workspace))
    yield store
    await store.close()


async def test_a_sessions_events_are_logged_but_for_those_answering_one_connection(
    sqlite: SqliteStore, tmp_path: Path
) -> None:
    session = stored_session(sqlite)
    await send(session, AddTask(task=TaskSpec(prompt="Add a flag.")))
    await publish(session, Notice(message="hi"))
    await publish(session, UsageChanged())
    await publish(session, SessionsListed(sessions=(), reason="asked"))
    await session.close()

    stored, sent = read_log(tmp_path / "state"), delivered(session)
    for kind in (Welcome, Catalog, Snapshot, UsageChanged, SessionsListed):
        assert payloads_of(kind, sent)
    assert stored == kept(sent)
    assert [type(envelope.payload) for envelope in kept(sent)] == [
        TasksChanged,
        CommandAccepted,
        Notice,
    ]


async def test_a_session_counts_on_from_the_highest_seq_logged() -> None:
    store = FakeStore()
    store.seq = 41
    session = stored_session(store)
    await greet(session)
    await session.close()

    assert delivered(session)[0].seq == 42


async def test_events_published_at_once_are_written_in_few_appends_and_all_arrive() -> None:
    store = FakeStore()
    session = stored_session(store)
    for n in range(50):
        session._events.put_nowait(Notice(message=f"n{n}"))
    await session.close()

    assert len(store.appends) < 50
    assert [envelope.payload for envelope in store.logged] == [
        Notice(message=f"n{n}") for n in range(50)
    ]


async def test_closing_writes_the_events_still_queued() -> None:
    store = FakeStore()
    session = stored_session(store)
    await publish(session, Notice(message="last"))
    await session.close()

    assert [envelope.payload for envelope in store.logged] == [Notice(message="last")]


async def test_a_failing_log_is_reported_once_and_the_frontend_gets_every_event() -> None:
    store = FakeStore()
    store.append_failure = "disk full"
    session = stored_session(store)
    for n in range(3):
        await publish(session, Notice(message=f"n{n}"))
        await asyncio.sleep(0.01)
    await session.close()

    sent = delivered(session)
    assert payloads_of(Notice, sent) == [Notice(message=f"n{n}") for n in range(3)]
    assert payloads_of(ErrorOccurred, sent) == [
        ErrorOccurred(code=SAVE_FAILED, message="disk full")
    ]
