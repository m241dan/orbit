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
    UNKNOWN,
    AddTask,
    Catalog,
    CommandAccepted,
    ErrorOccurred,
    Event,
    EventEnvelope,
    History,
    HistoryPage,
    Notice,
    OpenSession,
    SessionsListed,
    Snapshot,
    TasksChanged,
    TaskSpec,
    UsageChanged,
    Welcome,
    decode_event,
)
from tests.agent_fakes import COMMAND_ID, FakeStore, Ticks, greet, publish, refused, send
from tests.test_session_switch import opened, saved, store_with
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


async def test_recent_events_are_the_logged_ones_then_those_still_queued() -> None:
    log = GatedLog()
    log.logged = [notice(1), notice(2, "b")]
    writer = LogWriter(log, Published())
    writer.add("a", notice(3))
    await log.waiting.wait()  # 3 is being written.
    writer.add("b", notice(4, "b"))
    writer.add("a", notice(5))

    assert await writer.recent("a", 10) == [notice(1), notice(3), notice(5)]
    assert await writer.recent("a", 2) == [notice(3), notice(5)]
    log.gate.set()
    await writer.close()


async def test_recent_events_written_while_the_log_is_read_are_there_once() -> None:
    log = FakeStore()
    writer = LogWriter(log, Published())
    writer.add("a", notice(1))
    reading = asyncio.create_task(writer.recent("a", 10))
    await asyncio.sleep(0)  # The writer writes 1 while the log is read.
    writer.add("a", notice(2))

    assert await reading == [notice(1), notice(2)]
    await writer.close()
    assert log.logged == [notice(1), notice(2)]


async def test_events_before_a_seq_are_the_logged_ones_then_those_still_queued() -> None:
    log = GatedLog()
    log.logged = [notice(1), notice(2), notice(3, "b")]
    writer = LogWriter(log, Published())
    writer.add("a", notice(4))
    await log.waiting.wait()  # 4 is being written.
    writer.add("a", notice(5))
    writer.add("a", notice(6))

    assert await writer.before("a", 6, 10) == ([notice(1), notice(2), notice(4), notice(5)], False)
    assert await writer.before("a", 6, 2) == ([notice(4), notice(5)], True)
    assert await writer.before("a", 2, 1) == ([notice(1)], False)
    log.gate.set()
    await writer.close()


async def test_events_before_a_seq_say_more_remain_when_the_log_holds_more() -> None:
    log = FakeStore()
    log.logged = [notice(1), notice(2), notice(3)]
    writer = LogWriter(log, Published())

    assert await writer.before("a", 3, 1) == ([notice(2)], True)


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


# The transcript in the Snapshot


def snapshots(sent: Sequence[EventEnvelope]) -> list[Snapshot]:
    return payloads_of(Snapshot, sent)


async def test_a_snapshot_holds_the_sessions_last_500_logged_events_oldest_first() -> None:
    store = FakeStore()
    store.logged = [notice(seq, "session-1") for seq in range(1, 601)]
    store.logged.insert(300, notice(1000, "other"))
    session = stored_session(store)
    await greet(session)
    await session.close()

    [snapshot] = snapshots(delivered(session))
    assert snapshot.transcript == tuple(notice(seq, "session-1") for seq in range(101, 601))


async def test_events_still_queued_end_a_snapshots_transcript_once() -> None:
    store = GatedLog()
    store.logged = [notice(1, "session-1")]
    session = stored_session(store)
    await publish(session, Notice(message="being written"))
    await store.waiting.wait()
    await publish(session, Notice(message="queued"))
    await greet(session)
    store.gate.set()
    await session.close()

    [snapshot] = snapshots(delivered(session))
    assert [envelope.payload for envelope in snapshot.transcript] == [
        Notice(message="n1"),
        Notice(message="being written"),
        Notice(message="queued"),
    ]
    assert [envelope.seq for envelope in snapshot.transcript] == [1, 2, 3]


async def test_a_snapshot_without_a_store_has_no_transcript() -> None:
    session = AgentSession(clock=Ticks())
    await publish(session, Notice(message="hi"))
    await greet(session)
    await session.close()

    [snapshot] = snapshots(delivered(session))
    assert snapshot.transcript == ()


async def test_a_switch_snapshots_the_transcript_of_the_session_opened() -> None:
    store = store_with(saved("auth"))
    store.logged = [notice(1, "auth"), notice(2, "session-1")]
    session = stored_session(store)
    events = await opened(session, OpenSession(session_id="auth"))
    await session.close()

    [_, switched] = [event for event in events if isinstance(event, Snapshot)]
    assert switched.transcript == (notice(1, "auth"),)


async def test_a_session_opened_again_after_a_restart_shows_its_earlier_events(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    store = await SqliteStore.open(tmp_path / "state", str(workspace))
    first = stored_session(store)
    await send(first, AddTask(task=TaskSpec(prompt="Add a flag.")))
    await first.close()
    await store.close()

    store = await SqliteStore.open(tmp_path / "state", str(workspace))
    again = AgentSession(store=store, clock=Ticks(), new_id=lambda: "session-2")
    events = await opened(again, OpenSession(session_id="session-1"))
    await again.close()
    await store.close()

    [_, reopened] = [event for event in events if isinstance(event, Snapshot)]
    assert reopened.transcript == tuple(kept(delivered(first)))
    assert [type(envelope.payload) for envelope in reopened.transcript] == [
        TasksChanged,
        CommandAccepted,
    ]


# Paging through history


async def history(session: AgentSession, command: History) -> list[Event]:
    """What ``session`` answers ``command`` with: its page and its outcome."""
    await greet(session)
    delivered(session)
    await send(session, command)
    return [envelope.payload for envelope in delivered(session)]


async def test_history_pages_back_from_the_snapshot_to_the_sessions_start(
    sqlite: SqliteStore,
) -> None:
    await sqlite.append("session-1", [notice(seq, "session-1") for seq in range(1, 1201)])
    session = stored_session(sqlite)
    await greet(session)
    [snapshot] = snapshots(delivered(session))
    oldest = snapshot.transcript[0].seq

    first = await history(session, History(session="session-1", before_seq=oldest, limit=500))
    second = await history(session, History(session="session-1", before_seq=201, limit=500))
    await session.close()

    assert oldest == 701
    assert first == [
        HistoryPage(
            session="session-1",
            events=tuple(notice(seq, "session-1") for seq in range(201, 701)),
            more=True,
        ),
        CommandAccepted(command_id=COMMAND_ID),
    ]
    assert second == [
        HistoryPage(
            session="session-1",
            events=tuple(notice(seq, "session-1") for seq in range(1, 201)),
            more=False,
        ),
        CommandAccepted(command_id=COMMAND_ID),
    ]


async def test_history_pages_a_saved_session_other_than_the_one_open() -> None:
    store = store_with(saved("auth"))
    store.logged = [notice(1, "auth"), notice(2, "session-1")]
    session = stored_session(store)

    answer = await history(session, History(session="auth", before_seq=10))
    await session.close()

    assert answer == [
        HistoryPage(session="auth", events=(notice(1, "auth"),), more=False),
        CommandAccepted(command_id=COMMAND_ID),
    ]


async def test_history_of_a_session_that_isnt_this_workspaces_is_refused(
    sqlite: SqliteStore, tmp_path: Path
) -> None:
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    other = await SqliteStore.open(tmp_path / "state", str(other_workspace))
    await other.save(saved("elsewhere"))
    await other.append("elsewhere", [notice(1, "elsewhere")])
    await other.close()
    session = stored_session(sqlite)

    answer = await history(session, History(session="elsewhere", before_seq=10))
    await session.close()

    assert answer == [refused(UNKNOWN, "There's no saved session 'elsewhere'.")]


async def test_history_without_a_store_is_an_empty_last_page() -> None:
    session = AgentSession(clock=Ticks(), new_id=lambda: "session-1")
    await publish(session, Notice(message="hi"))

    answer = await history(session, History(session="session-1", before_seq=10))
    await session.close()

    assert answer == [
        HistoryPage(session="session-1", events=(), more=False),
        CommandAccepted(command_id=COMMAND_ID),
    ]


async def test_history_pages_are_never_logged() -> None:
    store = FakeStore()
    session = stored_session(store)

    await history(session, History(session="session-1", before_seq=10))
    await session.close()

    assert [type(envelope.payload) for envelope in store.logged] == [CommandAccepted]
