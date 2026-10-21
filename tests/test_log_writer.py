"""The writer keeping sessions' events in the store's log, and what the session logs through it."""

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from orbit.agent import WorkspaceHost
from orbit.agent.application.log_writer import LogWriter, Overflow
from orbit.agent.domain.replay import LogBounds
from orbit.agent.domain.storage import StorageSettings
from orbit.protocol import (
    INTERNAL,
    PROTOCOL_VERSION,
    SAVE_FAILED,
    STORAGE_FAILED,
    UNKNOWN,
    ActivateSession,
    AddTask,
    Catalog,
    CommandAccepted,
    CommandEnvelope,
    ErrorOccurred,
    Event,
    EventEnvelope,
    Hello,
    History,
    HistoryPage,
    Notice,
    Snapshot,
    TasksChanged,
    TaskSpec,
    UsageChanged,
    Welcome,
    decode_event,
)
from tests.agent_fakes import (
    ATTACH_ID,
    COMMAND_ID,
    FakeFallback,
    FakeStore,
    Ticks,
    agent_exited,
    attach_to,
    events_until,
    frontend,
    greet,
    publish,
    queued_envelopes,
    refused,
    save_used,
    send,
)
from tests.store_views import OwnView, open_view
from tests.test_event_log import big
from tests.test_session_switch import failing_startup, opened, saved, store_with, switching
from tests.test_sqlite_store import database


def notice(seq: int, session: str = "a") -> EventEnvelope:
    return EventEnvelope(seq=seq, session=session, payload=Notice(message=f"n{seq}"))


class Published:
    """The events a writer reported, in a list."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)


class FailsFor(FakeStore):
    """A log whose appends of the session ``failing`` fail."""

    def __init__(self, failing: str) -> None:
        super().__init__()
        self.failing = failing

    async def append(self, session_id: str, envelopes: Sequence[EventEnvelope]) -> int:
        if session_id == self.failing:
            raise RuntimeError("disk full")
        return await super().append(session_id, envelopes)


class FailingTrims(FakeStore):
    """A log whose trims fail."""

    async def trim(self, session_id: str, limit: int, target: int) -> None:
        raise RuntimeError("trim failed")


class GatedLog(FakeStore):
    """A log whose appends wait for ``gate``, with ``waiting`` set while one does."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.waiting = asyncio.Event()

    async def append(self, session_id: str, envelopes: Sequence[EventEnvelope]) -> int:
        self.waiting.set()
        await self.gate.wait()
        return await super().append(session_id, envelopes)


def stored_session(store: FakeStore | OwnView) -> WorkspaceHost:
    return WorkspaceHost(store=store, clock=Ticks(), new_id=lambda: "session-1")


def delivered(session: WorkspaceHost) -> list[EventEnvelope]:
    """The envelopes the session has queued for the frontend, taken from its queue."""
    return queued_envelopes(session)


def kept(sent: Sequence[EventEnvelope]) -> list[EventEnvelope]:
    """The envelopes of ``sent`` the log keeps: a session's, but for those answering one
    connection, the acceptance of the Attach greeting it among them."""
    connection = Welcome | Catalog | Snapshot
    attached = CommandAccepted(command_id=ATTACH_ID)
    return [
        e
        for e in sent
        if e.session is not None and not isinstance(e.payload, connection) and e.payload != attached
    ]


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


async def test_a_failed_append_stops_the_appending_and_is_told_once() -> None:
    log, failures = FakeStore(), list[str]()
    log.append_failure = "disk full"
    writer = LogWriter(
        log, Published(), overflow=Overflow(failed=lambda error: failures.append(str(error)))
    )
    writer.add("a", notice(1))
    await asyncio.sleep(0.01)
    log.append_failure = None
    writer.add("a", notice(2))
    await writer.close()

    assert failures == ["disk full"]
    assert log.logged == []


async def test_after_a_failed_append_the_envelopes_go_to_the_fallback() -> None:
    log, fallback = FakeStore(), FakeFallback()
    log.append_failure = "disk full"
    writer = LogWriter(log, Published(), overflow=Overflow(fallback))
    writer.add("a", notice(1))
    writer.add("b", notice(2, "b"))
    await asyncio.sleep(0.01)
    writer.add("a", notice(3))
    await writer.close()

    assert fallback.files == {"a": [notice(1), notice(3)], "b": [notice(2, "b")]}


async def test_a_session_appended_before_the_failing_one_stays_logged() -> None:
    log, fallback = FailsFor("b"), FakeFallback()
    writer = LogWriter(log, Published(), overflow=Overflow(fallback))
    writer.add("a", notice(1))
    writer.add("b", notice(2, "b"))
    await writer.close()

    assert log.logged == [notice(1)] and fallback.files == {"b": [notice(2, "b")]}


async def test_stopping_sends_what_queued_and_what_follows_to_the_fallback() -> None:
    log, fallback = GatedLog(), FakeFallback()
    writer = LogWriter(log, Published(), overflow=Overflow(fallback))
    writer.add("a", notice(1))
    await log.waiting.wait()
    writer.add("a", notice(2))
    stopping = asyncio.create_task(writer.stop("a"))
    await asyncio.sleep(0.01)
    writer.add("a", notice(3))
    log.gate.set()
    stopped = await stopping
    await writer.close()

    assert stopped == ([notice(2), notice(3)], FakeFallback.path("a"))
    assert log.logged == [notice(1)] and fallback.files == {"a": [notice(2), notice(3)]}


async def test_stopping_waits_for_the_append_in_flight_and_diverts_it_when_it_fails() -> None:
    log, fallback = GatedLog(), FakeFallback()
    writer = LogWriter(log, Published(), overflow=Overflow(fallback))
    writer.add("a", notice(1))
    await log.waiting.wait()
    writer.add("a", notice(2))
    stopping = asyncio.create_task(writer.stop("a"))
    await asyncio.sleep(0.01)
    assert not stopping.done()
    log.append_failure = "disk full"
    log.gate.set()

    assert await stopping == ([notice(1), notice(2)], FakeFallback.path("a"))
    await writer.close()
    assert log.logged == [] and fallback.files == {"a": [notice(1), notice(2)]}


async def test_stopping_during_an_append_leaves_the_batchs_other_sessions_unwritten() -> None:
    log, fallback = GatedLog(), FakeFallback()
    writer = LogWriter(log, Published(), log_bytes=1000, overflow=Overflow(fallback))
    writer.add("a", notice(1))
    writer.add("b", notice(2, "b"))
    await log.waiting.wait()
    stopping = asyncio.create_task(writer.stop("a"))
    await asyncio.sleep(0.01)
    log.gate.set()

    assert await stopping == ([notice(2, "b")], FakeFallback.path("a"))
    await writer.close()
    assert log.appends == [("a", [notice(1)])] and log.trims == []
    assert fallback.files == {"a": [], "b": [notice(2, "b")]}


async def test_stopping_with_nothing_queued_makes_the_sessions_fallback_file() -> None:
    fallback = FakeFallback()
    writer = LogWriter(FakeStore(), Published(), overflow=Overflow(fallback))

    assert await writer.stop("a") == ([], FakeFallback.path("a"))
    assert fallback.files == {"a": []}


async def test_stopping_says_when_the_fallback_cannot_be_written() -> None:
    fallback = FakeFallback()
    fallback.broken = True
    writer = LogWriter(FakeStore(), Published(), overflow=Overflow(fallback))
    writer.add("a", notice(1))

    assert await writer.stop("a") == ([notice(1)], None)


async def test_without_a_fallback_stopping_writes_nowhere() -> None:
    writer = LogWriter(FakeStore(), Published())
    writer.add("a", notice(1))

    assert await writer.stop("a") == ([notice(1)], None)


async def test_envelopes_sent_to_the_fallback_are_still_read_back() -> None:
    log = FakeStore()
    log.logged = [notice(1)]
    writer = LogWriter(log, Published(), overflow=Overflow(FakeFallback()))
    writer.add("a", notice(2))
    await writer.stop("a")

    assert await writer.recent("a", 10) == [notice(1), notice(2)]
    assert await writer.after("a", 0) == [notice(1), notice(2)]
    bounds = await writer.bounds("b", 2)
    assert bounds is not None and bounds.others_seq == 2
    await writer.close()


async def test_resuming_appends_the_parked_events_then_those_queued_and_removes_the_file() -> None:
    log, fallback = FakeStore(), FakeFallback()
    log.append_failure = "disk full"
    writer = LogWriter(log, Published(), overflow=Overflow(fallback))
    writer.add("a", notice(2))
    writer.add("a", notice(1))
    await asyncio.sleep(0.01)
    log.append_failure = None
    writer.add("a", notice(3))

    assert await writer.resume() == []
    assert log.appends == [("a", [notice(1), notice(2), notice(3)])]
    assert fallback.files == {}
    writer.add("a", notice(4))
    await writer.close()
    assert log.logged == [notice(1), notice(2), notice(3), notice(4)]


async def test_resuming_reports_the_spans_of_events_that_reached_no_fallback() -> None:
    log, fallback = FakeStore(), FakeFallback()
    writer = LogWriter(log, Published(), overflow=Overflow(fallback))
    await writer.stop("a")
    fallback.broken = True
    for seq in (1, 2):
        writer.add("a", notice(seq))
    await asyncio.sleep(0.01)
    fallback.broken = False
    writer.add("a", notice(3))
    await asyncio.sleep(0.01)
    writer.add("a", notice(4))

    assert await writer.resume() == [(1, 2)]
    assert log.logged == [notice(3), notice(4)]
    assert await writer.recent("a", 10) == [notice(3), notice(4)]
    await writer.close()


async def test_without_a_fallback_resuming_reports_every_diverted_event_lost() -> None:
    log = FakeStore()
    writer = LogWriter(log, Published())
    writer.add("a", notice(1))
    await writer.stop("a")

    assert await writer.resume() == [(1, 1)]
    assert log.logged == []
    await writer.close()


async def test_resuming_while_the_log_still_fails_keeps_diverting() -> None:
    log, fallback = FakeStore(), FakeFallback()
    log.append_failure = "still full"
    writer = LogWriter(log, Published(), overflow=Overflow(fallback))
    writer.add("a", notice(1))
    await asyncio.sleep(0.01)
    writer.add("a", notice(2))

    error = await writer.resume()
    assert isinstance(error, RuntimeError) and str(error) == "still full"
    writer.add("a", notice(3))
    await writer.close()
    assert fallback.files == {"a": [notice(1), notice(2), notice(3)]}
    assert await writer.stop("a") == ([notice(1), notice(2), notice(3)], FakeFallback.path("a"))


async def test_a_cancelled_resume_keeps_the_queued_events_and_stays_stopped() -> None:
    log, fallback = GatedLog(), FakeFallback()
    writer = LogWriter(log, Published(), overflow=Overflow(fallback))
    await writer.stop("a")
    writer.add("a", notice(1))
    await asyncio.sleep(0.01)
    resuming = asyncio.create_task(writer.resume())
    writer.add("a", notice(2))  # Queued as the resume starts, so it takes it to append.
    await log.waiting.wait()
    resuming.cancel()

    with pytest.raises(asyncio.CancelledError):
        await resuming
    assert await writer.recent("a", 10) == [notice(1), notice(2)]
    await asyncio.sleep(0.01)
    assert fallback.files == {"a": [notice(1), notice(2)]}
    log.gate.set()
    assert await writer.resume() == []
    await writer.close()
    assert log.logged == [notice(1), notice(2)] and fallback.files == {}


async def test_resuming_a_writer_that_never_stopped_does_nothing() -> None:
    log = FakeStore()
    writer = LogWriter(log, Published())

    assert await writer.resume() == []
    assert log.appends == []


async def test_events_on_their_way_to_the_fallback_as_it_resumes_are_appended_instead() -> None:
    log, fallback = GatedLog(), FakeFallback()
    writer = LogWriter(log, Published(), overflow=Overflow(fallback))
    await writer.stop("a")
    writer.add("a", notice(1))
    await asyncio.sleep(0.01)
    resuming = asyncio.create_task(writer.resume())
    await log.waiting.wait()
    writer.add("a", notice(2))
    await asyncio.sleep(0.01)
    log.gate.set()

    assert await resuming == []
    await writer.close()
    assert log.logged == [notice(1), notice(2)] and fallback.files == {}


async def test_a_failing_trim_is_reported_once_and_later_events_are_still_written() -> None:
    log, published = FailingTrims(), Published()
    writer = LogWriter(log, published, log_bytes=1000)
    writer.add("a", notice(1))
    await asyncio.sleep(0.01)
    writer.add("a", notice(2))
    await writer.close()

    assert published.events == [ErrorOccurred(code=SAVE_FAILED, message="trim failed")]
    assert log.logged == [notice(1), notice(2)]


async def test_forgetting_a_session_deletes_its_events_after_those_being_written() -> None:
    log = GatedLog()
    writer = LogWriter(log, Published())
    writer.add("a", notice(1))
    await log.waiting.wait()
    writer.forget("a")
    writer.add("b", notice(2, "b"))
    closing = asyncio.create_task(writer.close())
    log.gate.set()
    await closing

    assert log.forgotten == ["a"]
    assert log.logged == [notice(2, "b")]


async def test_forgetting_a_session_deletes_its_events_still_queued() -> None:
    log = FakeStore()
    writer = LogWriter(log, Published())
    writer.add("a", notice(1))
    writer.forget("a")
    await writer.close()

    assert log.appends == [("a", [notice(1)])]
    assert log.logged == []


async def test_a_failing_forget_is_reported_once_and_later_events_are_still_written() -> None:
    log, published = FakeStore(), Published()
    log.forget_failure = "read-only"
    writer = LogWriter(log, published)
    writer.forget("a")
    await asyncio.sleep(0.01)
    writer.forget("b")
    writer.add("c", notice(1, "c"))
    await writer.close()

    assert published.events == [ErrorOccurred(code=SAVE_FAILED, message="read-only")]
    assert log.logged == [notice(1, "c")]


async def test_a_session_forgotten_while_stopped_isnt_appended_on_resuming() -> None:
    log, fallback = FakeStore(), FakeFallback()
    writer = LogWriter(log, Published(), overflow=Overflow(fallback))
    await writer.stop("a")
    writer.add("a", notice(1))
    writer.add("b", notice(2, "b"))
    await asyncio.sleep(0.01)
    writer.add("a", notice(3))
    writer.forget("a")
    await asyncio.sleep(0.01)

    assert fallback.files == {"b": [notice(2, "b")]}
    assert await writer.recent("a", 10) == []
    assert await writer.resume() == []
    await writer.close()
    assert log.logged == [notice(2, "b")] and fallback.files == {}


async def test_a_session_forgotten_with_a_failing_append_isnt_appended_on_resuming() -> None:
    log, fallback = FakeStore(), FakeFallback()
    log.append_failure = "disk full"
    writer = LogWriter(log, Published(), overflow=Overflow(fallback))
    writer.add("a", notice(1))
    writer.add("b", notice(2, "b"))
    writer.forget("a")
    await asyncio.sleep(0.01)

    assert fallback.files == {"b": [notice(2, "b")]} and log.forgotten == ["a"]
    log.append_failure = None
    assert await writer.resume() == []
    await writer.close()
    assert log.logged == [notice(2, "b")] and log.forgotten == ["a"]


async def test_a_session_forgotten_while_stopped_loses_no_span_on_resuming() -> None:
    log = FakeStore()
    writer = LogWriter(log, Published())
    writer.add("a", notice(1))
    writer.add("b", notice(2, "b"))
    await writer.stop("a")
    writer.forget("a")
    await asyncio.sleep(0.01)

    assert await writer.resume() == [(2, 2)]
    await writer.close()


async def test_a_forget_made_while_stopped_deletes_the_logged_events_at_once() -> None:
    log = FakeStore()
    log.logged = [notice(1)]
    writer = LogWriter(log, Published(), overflow=Overflow(FakeFallback()))
    await writer.stop("a")
    writer.forget("a")
    await asyncio.sleep(0.01)

    assert log.logged == [] and log.forgotten == ["a"]
    assert await writer.resume() == []
    await writer.close()
    assert log.forgotten == ["a"]


async def test_a_forget_made_while_stopped_deletes_the_logged_events_on_closing() -> None:
    log = FakeStore()
    log.logged = [notice(1)]
    writer = LogWriter(log, Published(), overflow=Overflow(FakeFallback()))
    await writer.stop("a")
    writer.forget("a")
    await writer.close()

    assert log.logged == [] and log.forgotten == ["a"]


async def test_a_failed_forget_while_stopped_is_unreported_and_runs_once_resumed() -> None:
    log, published = FakeStore(), Published()
    log.logged = [notice(1)]
    log.append_failure = "disk full"
    log.forget_failure = "read-only"
    writer = LogWriter(log, published, overflow=Overflow(FakeFallback()))
    writer.add("b", notice(2, "b"))
    writer.forget("a")
    await asyncio.sleep(0.01)

    assert log.logged == [notice(1)] and log.forgotten == [] and published.events == []
    log.append_failure = log.forget_failure = None
    assert await writer.resume() == []
    await writer.close()
    assert log.logged == [notice(2, "b")] and log.forgotten == ["a"]


async def test_closing_a_stopped_writer_after_a_forget_leaves_no_fallback_file_for_it() -> None:
    log, fallback = FakeStore(), FakeFallback()
    log.append_failure = "disk full"
    writer = LogWriter(log, Published(), overflow=Overflow(fallback))
    writer.add("a", notice(1))
    writer.add("b", notice(2, "b"))
    await asyncio.sleep(0.01)
    assert set(fallback.files) == {"a", "b"}

    writer.forget("a")
    await writer.close()

    assert fallback.files == {"b": [notice(2, "b")]} and log.forgotten == ["a"]


async def test_resuming_after_a_forget_appends_the_other_sessions_parked_envelopes() -> None:
    log, fallback = FakeStore(), FakeFallback()
    log.append_failure = "disk full"
    writer = LogWriter(log, Published(), overflow=Overflow(fallback))
    writer.add("a", notice(1))
    writer.add("b", notice(2, "b"))
    await asyncio.sleep(0.01)
    writer.add("b", notice(3, "b"))
    writer.add("a", notice(4))
    writer.forget("a")
    await asyncio.sleep(0.01)
    log.append_failure = None

    assert await writer.resume() == []
    await writer.close()
    assert log.appends[-1] == ("b", [notice(2, "b"), notice(3, "b")])
    assert log.logged == [notice(2, "b"), notice(3, "b")] and log.forgotten == ["a"]
    assert fallback.files == {}


async def test_a_writer_closed_forgets_nothing() -> None:
    log = FakeStore()
    writer = LogWriter(log, Published())
    await writer.close()
    writer.forget("a")
    await asyncio.sleep(0.01)

    assert log.forgotten == []


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


async def test_events_after_a_seq_are_the_logged_ones_then_those_still_queued() -> None:
    log = GatedLog()
    log.logged = [notice(1), notice(2), notice(3, "b")]
    writer = LogWriter(log, Published())
    writer.add("a", notice(4))
    await log.waiting.wait()  # 4 is being written.
    writer.add("a", notice(5))
    writer.add("b", notice(6, "b"))

    assert await writer.after("a", 1) == [notice(2), notice(4), notice(5)]
    assert await writer.after("a", 4) == [notice(5)]
    log.gate.set()
    await writer.close()


async def test_bounds_count_the_events_still_queued_as_logged() -> None:
    log = GatedLog()
    log.logged = [notice(3), notice(4, "b")]
    writer = LogWriter(log, Published())
    writer.add("a", notice(5))
    await log.waiting.wait()  # 5 is being written.
    writer.add("b", notice(6, "b"))
    writer.add("c", notice(7, "c"))

    assert await writer.bounds("a", 7) == LogBounds(first_seq=3, others_seq=7, own_seq=5)
    assert await writer.bounds("a", 4) == LogBounds(first_seq=3, others_seq=7, own_seq=3)
    assert await writer.bounds("c", 7) == LogBounds(first_seq=7, others_seq=6, own_seq=7)
    assert await writer.bounds("c", 6) == LogBounds(first_seq=7, others_seq=6, own_seq=None)
    assert await writer.bounds("d", 7) == LogBounds(first_seq=None, others_seq=7, own_seq=None)
    log.gate.set()
    await writer.close()


async def test_with_a_limit_each_session_written_is_trimmed_after_its_append() -> None:
    log = FakeStore()
    writer = LogWriter(log, Published(), log_bytes=1000)
    for session, seq in [("a", 1), ("b", 2)]:
        writer.add(session, notice(seq, session))
    await writer.close()

    assert log.trims == [("a", 1000, 900), ("b", 1000, 900)]


async def test_without_a_limit_nothing_is_trimmed() -> None:
    log = FakeStore()
    writer = LogWriter(log, Published())
    writer.add("a", notice(1))
    await writer.close()

    assert log.appends and log.trims == []


async def test_a_session_whose_append_fails_isnt_trimmed() -> None:
    log = FakeStore()
    log.append_failure = "disk full"
    writer = LogWriter(log, Published(), log_bytes=1000)
    writer.add("a", notice(1))
    await writer.close()

    assert log.trims == []


async def test_whether_a_log_was_trimmed_is_the_logs() -> None:
    log = FakeStore()
    log.trimmed_logs = {"a"}
    writer = LogWriter(log, Published())

    assert await writer.trimmed("a") and not await writer.trimmed("b")


class Unreadable(FakeStore):
    """A log whose events can't be read back."""

    async def after(self, session_id: str, after_seq: int) -> list[EventEnvelope] | None:
        return None

    async def bounds(self, session_id: str, since: int) -> LogBounds | None:
        return None

    async def before(
        self, session_id: str, before_seq: int, limit: int
    ) -> tuple[list[EventEnvelope], bool]:
        raise RuntimeError("database is locked")


async def test_reads_before_a_seq_of_a_log_that_cant_be_read_raise() -> None:
    writer = LogWriter(Unreadable(), Published())
    writer.add("a", notice(1))

    with pytest.raises(RuntimeError, match="database is locked"):
        await writer.before("a", 10, 10)
    await writer.close()


async def test_reads_after_a_seq_of_a_log_that_cant_be_read_are_none() -> None:
    writer = LogWriter(Unreadable(), Published())
    writer.add("a", notice(1))

    assert await writer.after("a", 0) is None
    assert await writer.bounds("a", 1) is None
    await writer.close()


async def test_a_session_whose_log_cant_be_read_answers_since_with_a_snapshot() -> None:
    session = stored_session(Unreadable())
    hello = Hello(client="orbit-tests", protocol_version=PROTOCOL_VERSION, since=0)
    await frontend(session).send(CommandEnvelope(command_id=COMMAND_ID, payload=hello))
    await frontend(session).send(CommandEnvelope(command_id=ATTACH_ID, payload=attach_to(session)))
    await session.close()

    assert payloads_of(Snapshot, delivered(session))


# What the session logs


@pytest.fixture
async def sqlite(tmp_path: Path) -> AsyncIterator[OwnView]:
    workspace = tmp_path / "project"
    workspace.mkdir()
    store = await open_view(tmp_path / "state", workspace)
    yield store
    await store.close()


async def test_a_sessions_events_are_logged_but_for_those_answering_one_connection(
    sqlite: OwnView, tmp_path: Path
) -> None:
    session = stored_session(sqlite)
    await send(session, AddTask(task=TaskSpec(prompt="Add a flag.")))
    await publish(session, Notice(message="hi"))
    await publish(session, UsageChanged())
    await session.close()

    stored, sent = read_log(tmp_path / "state"), delivered(session)
    for kind in (Welcome, Catalog, Snapshot, UsageChanged):
        assert payloads_of(kind, sent)
    assert stored == kept(sent)
    assert [type(envelope.payload) for envelope in kept(sent)] == [
        TasksChanged,
        CommandAccepted,
        Notice,
    ]


async def test_a_session_closed_unused_leaves_no_events_behind(
    sqlite: OwnView, tmp_path: Path
) -> None:
    session = stored_session(sqlite)
    await greet(session)
    await publish(session, Notice(message="Workspace: project"))
    await session.close()

    assert read_log(tmp_path / "state") == []


async def test_a_session_used_after_its_startup_notices_keeps_them(
    sqlite: OwnView, tmp_path: Path
) -> None:
    session = stored_session(sqlite)
    await greet(session)
    await publish(session, Notice(message="Workspace: project"))
    await send(session, AddTask(task=TaskSpec(prompt="Add a flag.")))
    await session.close()

    stored = read_log(tmp_path / "state")
    assert stored == kept(delivered(session))
    assert Notice(message="Workspace: project") in [e.payload for e in stored if e is not None]


def gated_with_auth(queued: bool) -> GatedLog:
    """A gated log holding the saved session ``auth``, its gate open unless events are to stay
    ``queued`` until it's opened."""
    store = GatedLog()
    store.saved = [saved("auth")]
    if not queued:
        store.gate.set()
    return store


async def pick_auth(session: WorkspaceHost, store: GatedLog) -> None:
    """Pick the saved session ``auth`` in ``session``, then open ``store``'s gate and close."""
    await opened(session, ActivateSession(session="auth"))
    store.gate.set()
    await session.close()


@pytest.mark.parametrize("queued", [False, True])
async def test_picking_a_session_leaves_no_events_of_the_unsaved_one_left(queued: bool) -> None:
    store = gated_with_auth(queued)
    session, _ = switching(store, pick_session=True)
    await publish(session, Notice(message="Workspace: project"))
    await store.waiting.wait()
    await pick_auth(session, store)

    assert store.appends and [e for e in store.logged if e.session == "session-1"] == []
    assert store.forgotten == ["session-1"]


@pytest.mark.parametrize("queued", [False, True])
async def test_picking_a_session_leaves_no_events_of_the_saved_one_discarded(
    queued: bool,
) -> None:
    store = gated_with_auth(queued)
    session = failing_startup(store)
    await events_until(session, agent_exited("startup-check"))
    await store.waiting.wait()
    await pick_auth(session, store)

    assert store.deleted == ["session-1"]
    assert store.appends and [e for e in store.logged if e.session == "session-1"] == []


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
    await save_used(session)
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
    await save_used(session)
    await publish(session, Notice(message="last"))
    await session.close()

    assert [envelope.payload for envelope in store.logged] == [Notice(message="last")]


async def test_a_failing_log_halts_the_session_and_the_frontend_gets_every_event() -> None:
    store = FakeStore()
    store.append_failure = "disk full"
    session = stored_session(store)
    await greet(session)
    for n in range(3):
        await publish(session, Notice(message=f"n{n}"))
        await asyncio.sleep(0.01)
    await session.close()

    sent = delivered(session)
    assert payloads_of(Notice, sent) == [Notice(message=f"n{n}") for n in range(3)]
    [error] = payloads_of(ErrorOccurred, sent)
    assert error.code == STORAGE_FAILED and "disk full" in error.message


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
    session = WorkspaceHost(clock=Ticks())
    await publish(session, Notice(message="hi"))
    await greet(session)
    await session.close()

    [snapshot] = snapshots(delivered(session))
    assert snapshot.transcript == ()


async def test_a_switch_snapshots_the_transcript_of_the_session_opened() -> None:
    store = store_with(saved("auth"))
    store.logged = [notice(1, "auth"), notice(2, "session-1")]
    session = stored_session(store)
    events = await opened(session, ActivateSession(session="auth"))
    await session.close()

    [_, switched] = [event for event in events if isinstance(event, Snapshot)]
    assert switched.transcript == (notice(1, "auth"),)


async def test_a_session_opened_again_after_a_restart_shows_its_earlier_events(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    store = await open_view(tmp_path / "state", workspace)
    first = stored_session(store)
    await send(first, AddTask(task=TaskSpec(prompt="Add a flag.")))
    await first.close()
    await store.close()

    store = await open_view(tmp_path / "state", workspace)
    again = WorkspaceHost(store=store, clock=Ticks(), new_id=lambda: "session-2")
    events = await opened(again, ActivateSession(session="session-1"))
    await again.close()
    await store.close()

    [_, reopened] = [event for event in events if isinstance(event, Snapshot)]
    assert reopened.transcript == tuple(kept(delivered(first)))
    assert [type(envelope.payload) for envelope in reopened.transcript] == [
        TasksChanged,
        CommandAccepted,
    ]


async def opened_after_another_logged_it(tmp_path: Path) -> WorkspaceHost:
    """A session whose store opened while the log was empty, after another orbit's logged the
    saved session ``auth`` up to ``seq`` 50, having opened ``auth``, published a notice and
    closed."""
    workspace = tmp_path / "project"
    workspace.mkdir()
    this, other = [await open_view(tmp_path / "state", workspace) for _ in range(2)]
    await other.save(saved("auth"))
    await other.append("auth", [notice(seq, "auth") for seq in range(1, 51)])
    await other.close()
    session = WorkspaceHost(store=this, clock=Ticks(), new_id=lambda: "session-2")
    await opened(session, ActivateSession(session="auth"))
    await publish(session, Notice(message="later"))
    await session.close()
    await this.close()
    return session


def of_auth(envelopes: Sequence[EventEnvelope | None]) -> list[EventEnvelope]:
    """The envelopes of the session ``auth`` in ``envelopes``."""
    return [envelope for envelope in envelopes if envelope and envelope.session == "auth"]


async def test_a_session_another_orbit_logged_since_this_one_opened_goes_on_above_its_seqs(
    tmp_path: Path,
) -> None:
    session = await opened_after_another_logged_it(tmp_path)

    sent = of_auth(delivered(session))
    assert sent and min(envelope.seq for envelope in sent) > 50
    assert payloads_of(ErrorOccurred, sent) == []
    logged = of_auth(read_log(tmp_path / "state"))
    assert logged[:50] == [notice(seq, "auth") for seq in range(1, 51)]
    assert set(kept(sent)) <= set(logged[50:])
    assert Notice(message="later") in [envelope.payload for envelope in logged[50:]]


# Paging through history


async def history(session: WorkspaceHost, command: History) -> list[Event]:
    """What ``session`` answers ``command`` with: its page and its outcome."""
    await greet(session)
    delivered(session)
    await send(session, command)
    return [envelope.payload for envelope in delivered(session)]


async def test_history_pages_back_from_the_snapshot_to_the_sessions_start(
    sqlite: OwnView,
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
    sqlite: OwnView, tmp_path: Path
) -> None:
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    other = await open_view(tmp_path / "state", other_workspace)
    await other.save(saved("elsewhere"))
    await other.append("elsewhere", [notice(1, "elsewhere")])
    await other.close()
    session = stored_session(sqlite)

    answer = await history(session, History(session="elsewhere", before_seq=10))
    await session.close()

    assert answer == [refused(UNKNOWN, "There's no saved session 'elsewhere'.")]


async def test_history_of_a_session_whose_log_cant_be_read_is_refused() -> None:
    session = stored_session(Unreadable())

    answer = await history(session, History(session="session-1", before_seq=10))
    await session.close()

    assert answer == [refused(INTERNAL, "Orbit failed to handle History: database is locked")]


async def test_history_without_a_store_is_an_empty_last_page() -> None:
    session = WorkspaceHost(clock=Ticks(), new_id=lambda: "session-1")
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
    await save_used(session)

    await history(session, History(session="session-1", before_seq=10))
    await session.close()

    assert [type(envelope.payload) for envelope in store.logged] == [CommandAccepted]


async def test_history_of_a_trimmed_log_marks_only_its_oldest_page() -> None:
    store = store_with(saved("auth"))
    store.logged = [notice(seq, "auth") for seq in (1, 2)]
    store.trimmed_logs = {"auth"}
    session = stored_session(store)

    newer = await history(session, History(session="auth", before_seq=10, limit=1))
    oldest = await history(session, History(session="auth", before_seq=2))
    await session.close()

    assert newer[0] == HistoryPage(session="auth", events=(notice(2, "auth"),), more=True)
    assert oldest[0] == HistoryPage(
        session="auth", events=(notice(1, "auth"),), more=False, trimmed=True
    )


# Trimming the log


def log_size(state: Path, session_id: str) -> int:
    """The bytes of the session ``session_id``'s events in the database in ``state``."""
    with database(state) as connection:
        query = "SELECT coalesce(sum(size), 0) FROM events WHERE session_id = ?"
        (size,) = connection.execute(query, (session_id,)).fetchone()
    return int(size)


def trimming_session(store: OwnView, log_mb: int) -> WorkspaceHost:
    storage = StorageSettings(session_log_mb=log_mb)
    return WorkspaceHost(store=store, clock=Ticks(), new_id=lambda: "session-1", storage=storage)


async def test_a_session_log_over_its_limit_is_trimmed_after_the_next_batch(
    sqlite: OwnView, tmp_path: Path
) -> None:
    await sqlite.append("session-1", [big(seq, "session-1") for seq in range(1, 211)])
    await sqlite.append("other", [big(seq, "other") for seq in range(211, 421)])
    session = trimming_session(sqlite, 1)
    await save_used(session)
    await publish(session, Notice(message="hi"))
    await session.close()

    state = tmp_path / "state"
    kept = await sqlite.recent("session-1", 1000)
    assert log_size(state, "session-1") < 1024 * 1024 * 9 // 10
    assert payloads_of(Notice, kept)[-1] == Notice(message="hi") and kept[0].seq > 1
    assert len(await sqlite.recent("other", 1000)) == 210


async def test_with_no_limit_a_session_log_is_never_trimmed(
    sqlite: OwnView, tmp_path: Path
) -> None:
    await sqlite.append("session-1", [big(seq, "session-1") for seq in range(1, 211)])
    session = trimming_session(sqlite, 0)
    await save_used(session)
    await publish(session, Notice(message="hi"))
    await session.close()

    assert (await sqlite.recent("session-1", 1000))[0].seq == 1
    assert not await sqlite.trimmed("session-1")


async def test_after_trimming_the_oldest_page_is_marked_trimmed(sqlite: OwnView) -> None:
    await sqlite.append("session-1", [big(seq, "session-1") for seq in range(1, 211)])
    writing = trimming_session(sqlite, 1)
    await save_used(writing)
    await publish(writing, Notice(message="hi"))
    await writing.close()
    first = (await sqlite.recent("session-1", 1000))[0].seq
    session = trimming_session(sqlite, 1)

    answer = await history(session, History(session="session-1", before_seq=first + 1))
    await session.close()

    assert answer[0] == HistoryPage(
        session="session-1", events=(big(first, "session-1"),), more=False, trimmed=True
    )


async def hello_since(store: OwnView, since: int) -> list[EventEnvelope]:
    """What a session on ``store`` sends a frontend that saw the events up to ``since``."""
    session = trimming_session(store, 1)
    hello = Hello(client="orbit-tests", protocol_version=PROTOCOL_VERSION, since=since)
    await frontend(session).send(CommandEnvelope(command_id=COMMAND_ID, payload=hello))
    await frontend(session).send(CommandEnvelope(command_id=ATTACH_ID, payload=attach_to(session)))
    await session.close()
    return delivered(session)


async def trimmed_log(store: OwnView) -> int:
    """The ``seq`` of the oldest event kept of a log of ``session-1`` written to ``store``, after
    any logged before it, and trimmed."""
    await store.forget("session-1")
    start = (await store.latest_seq() or 0) + 1
    await store.append("session-1", [big(seq, "session-1") for seq in range(start, start + 210)])
    await store.trim("session-1", 1024 * 1024, 1024 * 1024 * 9 // 10)
    return (await store.recent("session-1", 1000))[0].seq


async def test_a_frontend_that_saw_only_trimmed_events_gets_a_fresh_snapshot(
    sqlite: OwnView,
) -> None:
    first = await trimmed_log(sqlite)
    assert snapshots(await hello_since(sqlite, first - 2))

    first = await trimmed_log(sqlite)  # The session before forgot its log.
    assert not snapshots(await hello_since(sqlite, first - 1))
