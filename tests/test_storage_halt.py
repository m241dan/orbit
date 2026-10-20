"""Halting the session when a write to the store fails: the explanation, the refusals, the work
cancelled and the unsaved events parked in a fallback file."""

import asyncio
import re
import stat
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from orbit.agent import AgentSession
from orbit.agent.application.keeper import SessionKeeper
from orbit.agent.application.outbox import Outbox
from orbit.agent.application.ports import StorageFailed
from orbit.agent.application.storage_halt import (
    UNKNOWN_DATABASE,
    HaltHooks,
    StorageHalt,
    fault_of,
)
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.sessions import SessionRecord
from orbit.agent.domain.storage_halt import (
    NEITHER_PLACE,
    FailedWrite,
    StorageErrorKind,
    StorageFault,
    halting_message,
)
from orbit.agent.external.fallback_files import FallbackFiles
from orbit.agent.external.sqlite_store import SqliteStore
from orbit.protocol import (
    PLANNER,
    PLANNER_LANE,
    STORAGE_FAILED,
    AddTask,
    BusyChanged,
    CommandAccepted,
    CommandRefused,
    ErrorOccurred,
    Event,
    EventEnvelope,
    History,
    HistoryPage,
    LogGap,
    Notice,
    ResumeStorage,
    RunTasks,
    SendMessage,
    TaskSpec,
    TurnEndReason,
    decode_event,
)
from tests.agent_fakes import (
    COMMAND_ID,
    START,
    Call,
    FakeFallback,
    FakeStore,
    FakeTree,
    Script,
    Steps,
    desk_of,
    frontend,
    greet,
    payloads,
    planner_turn,
    publish,
    refused,
    send,
    session_for,
    startup_of,
    tasks_of,
)
from tests.test_log_writer import GatedLog, payloads_of
from tests.test_planner_lane import gated_planner
from tests.test_review_lane import claims_done, suggests
from tests.test_sqlite_store import fill_up
from tests.test_startup_check import checked

TASK = TaskSpec(prompt="Add a flag.")
BIG = Notice(message="x" * 100_000)  # Too big for a database that can't grow.


def halted(event: Event | EventEnvelope) -> bool:
    payload = event.payload if isinstance(event, EventEnvelope) else event
    return isinstance(payload, ErrorOccurred) and payload.code == STORAGE_FAILED


async def envelopes_until(
    session: AgentSession, done: Callable[[list[EventEnvelope]], bool], timeout: float = 5
) -> list[EventEnvelope]:
    """The session's envelopes, from its Welcome on, up to the first that makes ``done`` hold
    of those so far."""
    await greet(session)
    sent: list[EventEnvelope] = []

    async def collect() -> None:
        async for envelope in frontend(session).events():
            sent.append(envelope)
            if done(sent):
                return

    await asyncio.wait_for(collect(), timeout)
    return sent


async def halt_error(session: AgentSession) -> ErrorOccurred:
    """The error the session halted with, once it's published."""
    sent = await envelopes_until(session, lambda sent: halted(sent[-1]))
    error = sent[-1].payload
    assert isinstance(error, ErrorOccurred)
    return error


def work_stopped_after_halt(sent: list[EventEnvelope]) -> bool:
    """Whether the session halted and then its work lane went idle."""
    halts = [n for n, envelope in enumerate(sent) if halted(envelope)]
    idle = BusyChanged(busy=False, lane="work")
    return bool(halts) and idle in [envelope.payload for envelope in sent[halts[0] :]]


def lines_of(path: Path) -> list[EventEnvelope]:
    decoded = [decode_event(line) for line in path.read_text().splitlines()]
    assert None not in decoded
    return [envelope for envelope in decoded if envelope is not None]


def seq_span_in(message: str) -> tuple[int, int]:
    found = re.search(r"seq (\d+) to (\d+)", message)
    assert found is not None, message
    return int(found[1]), int(found[2])


def waiting_agent(started: asyncio.Event) -> Script:
    async def waits(call: Call) -> TurnEndReason:
        await call("get_task")
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    return waits


@pytest.fixture
def unwritable(tmp_path: Path) -> Iterator[Path]:
    """A state directory that can't be written."""
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o500)
    yield state
    state.chmod(0o700)


# A full disk, end to end


@dataclass(frozen=True)
class FullDisk:
    """A session halted by a full disk while its agent worked: what it ``sent`` up to its work
    stopping, the ``error`` it halted with, the ``seq`` of the event that didn't fit, the
    ``database`` and the fallback ``file``."""

    session: AgentSession
    sent: list[EventEnvelope]
    error: ErrorOccurred
    seq: int
    database: Path
    file: Path


@pytest.fixture
async def full_disk(tmp_path: Path) -> AsyncIterator[FullDisk]:
    state = tmp_path / "state"
    store = await SqliteStore.open(state, str(tmp_path))
    started = asyncio.Event()
    fallback = FallbackFiles(state, store.workspace_id, tmp_path / ".orbit")
    session, _, _ = session_for(waiting_agent(started), store=store, fallback=fallback)
    await send(session, AddTask(task=TASK))
    await send(session, RunTasks())
    await asyncio.wait_for(started.wait(), 5)
    await fill_up(store)
    await publish(session, BIG)
    sent = await envelopes_until(session, work_stopped_after_halt)
    [error] = [envelope.payload for envelope in sent if halted(envelope)]
    assert isinstance(error, ErrorOccurred)
    [seq] = [envelope.seq for envelope in sent if envelope.payload == BIG]
    file = state / "fallback" / f"{store.workspace_id}-session-1.jsonl"
    yield FullDisk(session, sent, error, seq, state / "orbit.db", file)
    await session.close()
    await store.close()


async def test_a_full_disk_halts_the_session_explaining_why(full_disk: FullDisk) -> None:
    message = full_disk.error.message

    assert f"saving the event log to {full_disk.database} failed" in message
    assert "database or disk is full" in message
    assert "free space" in message
    assert f"were written to {full_disk.file}" in message
    first, last = seq_span_in(message)
    assert first <= full_disk.seq <= last


async def test_a_full_disk_cancels_the_work_and_refuses_commands(full_disk: FullDisk) -> None:
    desk = desk_of(full_disk.session)
    assert desk is not None and desk.running == ()

    await send(full_disk.session, RunTasks())
    assert await anext(payloads(full_disk.session)) == refused(
        STORAGE_FAILED, full_disk.error.message
    )


async def test_a_full_disk_parks_the_unsaved_events_in_an_owner_only_file(
    full_disk: FullDisk,
) -> None:
    await full_disk.session.close()

    parked = lines_of(full_disk.file)
    assert full_disk.seq in [envelope.seq for envelope in parked]
    assert any(halted(envelope) for envelope in parked)
    assert stat.S_IMODE(full_disk.file.stat().st_mode) == 0o600


# Where the unsaved events go


async def test_with_the_state_directory_unwritable_events_go_to_the_repository(
    tmp_path: Path, unwritable: Path
) -> None:
    store = FakeStore()
    store.append_failure = "disk full"
    repository = tmp_path / "repo" / ".git" / "orbit"
    session, _, _ = session_for(store=store, fallback=FallbackFiles(unwritable, "w1", repository))
    await send(session, AddTask(task=TASK))  # Saved, so closing it keeps its events.
    await publish(session, Notice(message="lost?"))
    error = await halt_error(session)
    store.append_failure = None
    await publish(session, Notice(message="later"))
    await session.close()

    file = repository / "fallback" / "session-1.jsonl"
    assert f"were written to {file}" in error.message
    assert Notice(message="lost?") in [envelope.payload for envelope in lines_of(file)]
    assert Notice(message="later") in [envelope.payload for envelope in lines_of(file)]
    assert store.logged == []


async def test_when_neither_place_can_be_written_the_explanation_says_so(tmp_path: Path) -> None:
    store = FakeStore()
    store.append_failure = "disk full"
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, so nothing can be made under it")
    fallback = FallbackFiles(blocked / "state", "w1", blocked / "repo")
    session, _, _ = session_for(store=store, fallback=fallback)
    await publish(session, Notice(message="lost"))
    error = await halt_error(session)
    await session.close()

    assert f"weren't written anywhere: {NEITHER_PLACE}" in error.message


# A failed session-record save


async def test_a_failed_session_record_save_halts_the_session_the_same_way() -> None:
    store = FakeStore()
    store.failure = "Couldn't save the session: disk full"
    fallback = FakeFallback()
    session, _, _ = session_for(store=store, fallback=fallback)
    await send(session, AddTask(task=TASK))
    error = await halt_error(session)

    assert f"saving the session record to {UNKNOWN_DATABASE} failed" in error.message
    assert "Couldn't save the session: disk full" in error.message
    await send(session, AddTask(task=TASK))
    assert await anext(payloads(session)) == refused(STORAGE_FAILED, error.message)
    await session.close()
    assert store.saved == []
    assert any(halted(envelope) for envelope in fallback.files["session-1"])


class FullStore(FakeStore):
    """A store holding the session ``other``, whose session saves fail as a full disk's do."""

    def __init__(self) -> None:
        super().__init__()
        self.saved = [SessionRecord(id="other", name="other", created=START, last_active=START)]

    async def save(self, record: SessionRecord) -> None:
        raise StorageFailed(
            "Couldn't save the session other: database or disk is full",
            "/state/orbit.db",
            "database or disk is full",
            StorageErrorKind.DISK_FULL,
        )


async def test_a_failed_rename_halts_the_session_and_lets_go_of_its_lock() -> None:
    published: list[Event] = []

    async def record(event: Event) -> None:
        published.append(event)

    store = FullStore()
    halt = StorageHalt(record)
    keeper = SessionKeeper(store, record, halt)
    await keeper.rename_saved("other", "Renamed")
    while not published:
        await asyncio.sleep(0)
    await halt.close()

    assert halt.halted and store.released == ["other"]
    [error] = published
    assert isinstance(error, ErrorOccurred) and error.code == STORAGE_FAILED
    assert "saving the session record to /state/orbit.db failed" in error.message


async def test_a_halt_during_an_append_that_then_fails_counts_its_events_unsaved() -> None:
    store, fallback = GatedLog(), FakeFallback()
    session, _, _ = session_for(store=store, fallback=fallback)
    await greet(session)
    await publish(session, Notice(message="blocked"))
    await store.waiting.wait()
    store.failure = "Couldn't save the session: disk full"
    await send(session, AddTask(task=TASK))
    await asyncio.sleep(0.01)
    store.append_failure = "disk full"
    store.gate.set()
    error = await halt_error(session)
    await session.close()

    [blocked] = [e for e in fallback.files["session-1"] if e.payload == Notice(message="blocked")]
    first, last = seq_span_in(error.message)
    assert first <= blocked.seq <= last


async def test_a_snapshot_taken_while_halted_carries_the_explanation() -> None:
    store = FakeStore()
    session, _, _ = session_for(store=store, fallback=FakeFallback())
    await greet(session)
    assert (await session._snapshot()).storage_halt is None
    store.failure = "full"
    await send(session, AddTask(task=TASK))
    error = await halt_error(session)

    assert (await session._snapshot()).storage_halt == error.message
    await session.close()


# The work cancelled


async def test_halting_cancels_the_planners_turn() -> None:
    store = FakeStore()
    session, _, launcher = session_for(store=store, fallback=FakeFallback())
    launcher.planner_turns = [gated_planner(asyncio.Event())]
    await send(session, SendMessage(agent=PLANNER, text="Plan it."))
    turn = planner_turn(session)
    assert turn is not None and not turn.done()
    store.append_failure = "disk full"
    await publish(session, Notice(message="now"))
    sent = await envelopes_until(
        session, lambda sent: sent[-1].payload == BusyChanged(busy=False, lane=PLANNER_LANE)
    )
    await session.close()

    assert any(halted(envelope) for envelope in sent)
    assert turn.result() == "cancelled"


async def test_halting_cancels_the_startup_check() -> None:
    steps = Steps(True)
    steps.gate = asyncio.Event()
    store = FakeStore()
    store.append_failure = "disk full"
    session, _, _ = session_for(
        verification=checked(steps), tree=FakeTree(), check_at_startup=True, store=store
    )
    await halt_error(session)
    startup = startup_of(session)
    assert startup is not None
    while startup.running:
        await asyncio.sleep(0.01)
    await session.close()

    assert steps.results == [True]  # The step never finished.


async def test_a_review_ending_while_halted_starts_no_work_on_its_fixes() -> None:
    store, sessions = GatedLog(), list[AgentSession]()

    async def suggests_then_halts(call: Call) -> TurnEndReason:
        await call("get_task")
        await call("suggest_task", title="Add tests", prompt="Please add tests.")
        await call("complete_task", summary="Reviewed")
        sessions[0]._halt.fail(FailedWrite.EVENT_LOG, RuntimeError("disk full"))
        return "completed"

    session, _, launcher = session_for(
        claims_done,
        claims_done,
        reviewers=(suggests_then_halts,),
        tree=FakeTree(),
        review=True,
        store=store,
    )
    sessions.append(session)
    await send(session, AddTask(task=TASK))
    await send(session, RunTasks())
    board = tasks_of(session).board
    while not any(t.task.role == "reviewer" and t.status == "completed" for t in board.tasks):
        await asyncio.sleep(0.01)  # The halt waits on the append in flight meanwhile.
    await asyncio.sleep(0.05)
    store.gate.set()
    await halt_error(session)
    await session.close()

    coders = [la for la in launcher.launches if la.agent.role == "coder"]
    assert len(coders) == 1  # The user's task, but none for the fix.


async def test_a_fix_cancelled_by_the_halt_leaves_its_batchs_review_waiting() -> None:
    started = asyncio.Event()
    session, _, launcher = session_for(
        claims_done,
        claims_done,
        waiting_agent(started),
        reviewers=(suggests("Add tests", "Add docs"),),
        tree=FakeTree(),
        review=True,
        store=FakeStore(),
        fallback=FakeFallback(),
    )
    await send(session, AddTask(task=TASK))
    await send(session, RunTasks())
    await asyncio.wait_for(started.wait(), 5)  # The last fix runs; its end makes a review due.
    session._halt.fail(FailedWrite.EVENT_LOG, RuntimeError("disk full"))
    await envelopes_until(session, work_stopped_after_halt)
    await asyncio.sleep(0.05)
    reviewers = [la for la in launcher.launches if la.agent.role == "reviewer"]
    reviews = [t for t in tasks_of(session).board.tasks if t.task.role == "reviewer"]
    await session.close()

    assert len(reviewers) == 1  # The first review, but none of the fixes.
    assert [t.status for t in reviews] == ["completed", "pending"]


# The halt itself


def test_a_store_failure_is_described_as_the_store_reported_it() -> None:
    error = StorageFailed(
        "Couldn't save", "/state/orbit.db", "disk I/O error", StorageErrorKind.UNKNOWN
    )

    assert fault_of(FailedWrite.EVENT_LOG, error) == StorageFault(
        FailedWrite.EVENT_LOG, "/state/orbit.db", "disk I/O error", StorageErrorKind.UNKNOWN
    )


async def test_until_the_explanation_is_made_the_halt_says_what_failed() -> None:
    halt = StorageHalt(lambda event: asyncio.sleep(0))
    assert halt.message is None and not halt.halted
    halt.check()
    halt.fail(FailedWrite.SESSION_RECORD, RuntimeError("full"))

    assert halt.halted and halt.message == halting_message(FailedWrite.SESSION_RECORD)
    with pytest.raises(Refused) as refusal:
        halt.check()
    assert refusal.value.code == STORAGE_FAILED
    await halt.close()


async def test_a_session_halts_once_however_many_writes_fail() -> None:
    published: list[Event] = []

    async def record(event: Event) -> None:
        published.append(event)

    halt = StorageHalt(record)
    halt.fail(FailedWrite.EVENT_LOG, RuntimeError("first"))
    halt.fail(FailedWrite.SESSION_RECORD, RuntimeError("second"))
    while halt.message == halting_message(FailedWrite.EVENT_LOG):
        await asyncio.sleep(0)
    await halt.close()

    [error] = published
    assert isinstance(error, ErrorOccurred) and "first" in error.message
    assert "second" not in error.message


async def test_once_closed_a_failure_only_marks_the_session_halted() -> None:
    published: list[Event] = []

    async def record(event: Event) -> None:
        published.append(event)

    halt = StorageHalt(record)
    await halt.close()
    halt.fail(FailedWrite.EVENT_LOG, RuntimeError("late"))
    await asyncio.sleep(0.01)

    assert halt.halted and published == []


async def test_an_outbox_without_a_log_diverts_and_loses_nothing() -> None:
    outbox = Outbox(lambda: "a")

    assert await outbox.stop_log("a") == ([], None)
    assert await outbox.resume_log() == []


# Resuming


async def outcome_of(
    session: AgentSession, command: ResumeStorage | RunTasks | History
) -> list[EventEnvelope]:
    """The envelopes the session sends once ``command`` is sent, up to its outcome."""
    await send(session, command)
    sent: list[EventEnvelope] = []

    async def collect() -> None:
        async for envelope in frontend(session).events():
            sent.append(envelope)
            payload = envelope.payload
            if isinstance(payload, CommandAccepted | CommandRefused) and (
                payload.command_id == COMMAND_ID
            ):
                return

    await asyncio.wait_for(collect(), 5)
    return sent


ACCEPTED = CommandAccepted(command_id=COMMAND_ID)


async def halted_session(store: FakeStore, fallback: FakeFallback) -> AgentSession:
    """A session over ``store`` halted by its failing appends, with three notices published
    first."""
    store.append_failure = "disk full"
    session, _, _ = session_for(store=store, fallback=fallback)
    for n in range(3):
        await publish(session, Notice(message=f"n{n}"))
    await halt_error(session)
    return session


async def test_resuming_once_writing_works_logs_the_parked_events_and_lifts_the_halt() -> None:
    store, fallback = FakeStore(), FakeFallback()
    session = await halted_session(store, fallback)
    parked = list(fallback.files["session-1"])
    store.append_failure = None

    assert (await outcome_of(session, ResumeStorage()))[-1].payload == ACCEPTED
    assert store.logged[: len(parked)] == sorted(parked, key=lambda envelope: envelope.seq)
    assert any(halted(envelope) for envelope in store.logged)
    assert fallback.files == {}
    assert (await session._snapshot()).storage_halt is None
    assert (await outcome_of(session, RunTasks()))[-1].payload == ACCEPTED
    await session.close()
    assert [envelope.seq for envelope in store.logged] == sorted(
        {envelope.seq for envelope in store.logged}
    )


async def test_resuming_logs_a_gap_for_events_that_reached_no_fallback() -> None:
    store, fallback = FakeStore(), FakeFallback()
    fallback.broken = True
    session = await halted_session(store, fallback)
    store.append_failure = None
    lost = [envelope.seq for envelope in await session._events.transcript("session-1")]

    [gap] = payloads_of(LogGap, await outcome_of(session, ResumeStorage()))
    assert gap == LogGap(first_seq=lost[0], last_seq=lost[-1])
    page = await outcome_of(session, History(session="session-1", before_seq=1000))
    [history] = payloads_of(HistoryPage, page)
    assert gap in payloads_of(LogGap, history.events)
    await session.close()
    assert gap in payloads_of(LogGap, [e for _, appended in store.appends for e in appended])


async def test_resuming_while_writing_still_fails_is_refused_and_the_session_stays_halted() -> None:
    store, fallback = FakeStore(), FakeFallback()
    session = await halted_session(store, fallback)
    store.append_failure = "still full"

    refusal = (await outcome_of(session, ResumeStorage()))[-1].payload
    assert isinstance(refusal, CommandRefused) and refusal.code == STORAGE_FAILED
    assert "still full" in refusal.message
    assert (await session._snapshot()).storage_halt == refusal.message
    assert (await outcome_of(session, RunTasks()))[-1].payload == refused(
        STORAGE_FAILED, refusal.message
    )
    await session.close()


async def test_resuming_while_the_session_record_still_fails_names_it() -> None:
    store = FakeStore()
    store.failure = "record still full"
    session, _, _ = session_for(store=store, fallback=FakeFallback())
    await send(session, AddTask(task=TASK))
    await halt_error(session)

    refusal = (await outcome_of(session, ResumeStorage()))[-1].payload
    assert isinstance(refusal, CommandRefused) and refusal.code == STORAGE_FAILED
    assert "saving the session record" in refusal.message
    store.failure = None
    assert (await outcome_of(session, ResumeStorage()))[-1].payload == ACCEPTED
    assert [record.tasks[0].task for record in store.saved] == [TASK]
    await session.close()


async def test_resuming_while_the_review_count_still_fails_names_it() -> None:
    store = FakeStore()
    store.count_failure = "count still full"
    session, _, _ = session_for(store=store, fallback=FakeFallback())
    await session._review_count.load()
    await session._review_count.save(3)
    await halt_error(session)

    refusal = (await outcome_of(session, ResumeStorage()))[-1].payload
    assert isinstance(refusal, CommandRefused) and "saving the review count" in refusal.message
    await session.close()


async def test_resuming_a_session_that_isnt_halted_is_accepted_and_does_nothing() -> None:
    store = FakeStore()
    session, _, _ = session_for(store=store, fallback=FakeFallback())

    assert (await outcome_of(session, ResumeStorage()))[-1].payload == ACCEPTED
    await session.close()


async def test_resuming_while_the_halt_is_still_under_way_is_refused() -> None:
    halt = StorageHalt(lambda event: asyncio.sleep(0))
    halt.fail(FailedWrite.EVENT_LOG, RuntimeError("full"))

    with pytest.raises(Refused) as refusal:
        await halt.resume()
    assert str(refusal.value) == halting_message(FailedWrite.EVENT_LOG)
    await halt.close()


async def test_a_resumed_halt_halts_again_on_the_next_failure() -> None:
    published: list[Event] = []

    async def record(event: Event) -> None:
        published.append(event)

    halt = StorageHalt(record, HaltHooks())
    halt.fail(FailedWrite.EVENT_LOG, RuntimeError("first"))
    while halt.message == halting_message(FailedWrite.EVENT_LOG):
        await asyncio.sleep(0)
    await asyncio.sleep(0.01)
    await halt.resume()
    assert not halt.halted
    halt.fail(FailedWrite.EVENT_LOG, RuntimeError("second"))
    while halt.message == halting_message(FailedWrite.EVENT_LOG):
        await asyncio.sleep(0)
    await halt.close()

    errors = [event.message for event in published if isinstance(event, ErrorOccurred)]
    assert ["second" in message for message in errors] == [False, True]


class OnceWorkingStore(FakeStore):
    """A store whose appends work once ``appends_left`` is set, for that many appends, and
    then fail again for want of space."""

    def __init__(self) -> None:
        super().__init__()
        self.appends_left: int | None = None

    async def append(self, session_id: str, envelopes: Sequence[EventEnvelope]) -> int:
        if self.appends_left is not None:
            self.append_failure = None if self.appends_left else "full again"
            self.appends_left = max(self.appends_left - 1, 0)
        return await super().append(session_id, envelopes)


async def test_a_failure_while_the_halt_is_lifted_halts_the_session_again() -> None:
    store, fallback = OnceWorkingStore(), FakeFallback()
    session = await halted_session(store, fallback)
    store.appends_left = 1  # Enough for the parked events, not the next.

    await send(session, ResumeStorage())
    error = await halt_error(session)
    assert "full again" in error.message
    assert (await session._snapshot()).storage_halt == error.message
    assert (await outcome_of(session, RunTasks()))[-1].payload == refused(
        STORAGE_FAILED, error.message
    )
    await session.close()


async def test_a_halt_lifted_despite_failures_halts_again_on_the_first() -> None:
    published: list[Event] = []

    async def record(event: Event) -> None:
        published.append(event)

    async def retry() -> tuple[FailedWrite, RuntimeError] | None:
        halt.fail(FailedWrite.EVENT_LOG, RuntimeError("again"))
        halt.fail(FailedWrite.SESSION_RECORD, RuntimeError("later"))
        return None

    halt = StorageHalt(record, HaltHooks(retry=retry))
    halt.fail(FailedWrite.EVENT_LOG, RuntimeError("first"))
    while halt.message == halting_message(FailedWrite.EVENT_LOG):
        await asyncio.sleep(0)
    await asyncio.sleep(0.01)
    await halt.resume()
    assert halt.halted
    while halt.message == halting_message(FailedWrite.EVENT_LOG):
        await asyncio.sleep(0)
    await halt.close()

    errors = [event.message for event in published if isinstance(event, ErrorOccurred)]
    [_, again] = errors
    assert "again" in again
    assert "session record" not in again
