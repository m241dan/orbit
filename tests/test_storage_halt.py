"""Halting the session when a write to the store fails: the explanation, the refusals, the work
cancelled and the unsaved events parked in a fallback file."""

import asyncio
import re
import stat
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from orbit.agent import WorkspaceHost
from orbit.agent.application.keeper import SessionKeeper
from orbit.agent.application.outbox import Outbox
from orbit.agent.application.ports import StorageFailed, StoreFailed
from orbit.agent.application.storage_halt import (
    UNKNOWN_DATABASE,
    HaltHooks,
    StorageHalt,
    fault_of,
    written,
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
from orbit.protocol import (
    PLANNER,
    PLANNER_LANE,
    REVIEWER,
    STORAGE_FAILED,
    AddTask,
    AgentExited,
    BusyChanged,
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
    Usage,
    decode_event,
)
from tests.agent_fakes import (
    ACCEPTED,
    START,
    Call,
    FakeFallback,
    FakeLedger,
    FakeStore,
    FakeTree,
    GatedLog,
    Script,
    Steps,
    agent_exited,
    chats,
    checked,
    claims_done,
    desk_of,
    events_until,
    frontend,
    gated_planner,
    greet,
    halt_of,
    idle,
    install_retry,
    lane_of,
    outcome_of,
    payloads,
    payloads_of,
    planner_turn,
    publish,
    refused,
    resume_storage,
    review_count_of,
    send,
    session_for,
    settled,
    snapshot_of,
    startup_of,
    suggests,
    tasks_of,
    turns_of,
)
from tests.server_fakes import TASK
from tests.store_views import fill_up, open_view

BIG = Notice(message="x" * 100_000)  # Too big for a database that can't grow.


def halted(event: Event | EventEnvelope) -> bool:
    payload = event.payload if isinstance(event, EventEnvelope) else event
    return isinstance(payload, ErrorOccurred) and payload.code == STORAGE_FAILED


async def envelopes_until(
    session: WorkspaceHost, done: Callable[[list[EventEnvelope]], bool], timeout: float = 5
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


async def halt_error(session: WorkspaceHost) -> ErrorOccurred:
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

    session: WorkspaceHost
    sent: list[EventEnvelope]
    error: ErrorOccurred
    seq: int
    database: Path
    file: Path


@pytest.fixture
async def full_disk(tmp_path: Path) -> AsyncIterator[FullDisk]:
    state = tmp_path / "state"
    store = await open_view(state, tmp_path)
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

    file = repository / "fallback" / "w1-session-1.jsonl"
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
    assert (await snapshot_of(session)).storage_halt is None
    store.failure = "full"
    await send(session, AddTask(task=TASK))
    error = await halt_error(session)

    assert (await snapshot_of(session)).storage_halt == error.message
    await session.close()


async def test_the_session_says_whether_storage_is_halted() -> None:
    store = FakeStore()
    session, _, _ = session_for(store=store, fallback=FakeFallback())
    await greet(session)
    assert not session.storage_halted
    store.failure = "full"
    await send(session, AddTask(task=TASK))
    await halt_error(session)

    assert session.storage_halted
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


async def test_a_review_lane_failing_to_stop_still_lets_halting_cancel_the_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    store = FakeStore()
    session, _, launcher = session_for(
        waiting_agent(started), tree=FakeTree(), review=True, store=store, fallback=FakeFallback()
    )

    async def fails() -> None:
        raise RuntimeError("The review lane failed to stop.")

    monkeypatch.setattr(lane_of(session), "stop", fails)
    launcher.planner_turns = [gated_planner(asyncio.Event())]
    await send(session, AddTask(task=TASK))
    await send(session, RunTasks())
    await asyncio.wait_for(started.wait(), 5)
    await send(session, SendMessage(agent=PLANNER, text="Plan it."))
    turn = planner_turn(session)
    assert turn is not None
    store.append_failure = "disk full"
    await publish(session, Notice(message="now"))
    sent = await envelopes_until(session, lambda sent: "failed to stop" in str(sent[-1].payload))
    working, planning = turns_of(session).running, not turn.done()
    await session.close()

    assert any(halted(envelope) for envelope in sent)
    assert not working and not planning and turn.result() == "cancelled"


async def test_a_review_ending_while_halted_starts_no_work_on_its_fixes() -> None:
    store, sessions = GatedLog(), list[WorkspaceHost]()

    async def suggests_then_halts(call: Call) -> TurnEndReason:
        await call("get_task")
        await call("suggest_task", title="Add tests", prompt="Please add tests.")
        await call("complete_task", summary="Reviewed")
        halt_of(sessions[0]).fail(FailedWrite.EVENT_LOG, StoreFailed("disk full"))
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


async def test_a_fix_cancelled_by_the_halt_leaves_its_batch_waiting_for_it() -> None:
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
    await asyncio.wait_for(started.wait(), 5)  # The last fix runs; its end would make a review due.
    planned = lane_of(session).planner.snapshot()
    halt_of(session).fail(FailedWrite.EVENT_LOG, StoreFailed("disk full"))
    await envelopes_until(session, work_stopped_after_halt)
    await asyncio.sleep(0.05)
    reviewers = [la for la in launcher.launches if la.agent.role == "reviewer"]
    board = tasks_of(session).board
    after = lane_of(session).planner.snapshot()
    await session.close()

    assert len(reviewers) == 1  # The first review, but none of the fixes.
    assert [(t.task.role, t.status) for t in board.tasks] == [
        ("coder", "completed"),
        ("reviewer", "completed"),
        ("coder", "completed"),
        ("coder", "cancelled"),
    ]
    assert after == planned  # The batch still waits for its fix, which runs again next.
    assert board.next_runnable() == board.tasks[-1].id


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
    halt.fail(FailedWrite.SESSION_RECORD, StoreFailed("full"))

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
    halt.fail(FailedWrite.EVENT_LOG, StoreFailed("first"))
    halt.fail(FailedWrite.SESSION_RECORD, StoreFailed("second"))
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
    halt.fail(FailedWrite.EVENT_LOG, StoreFailed("late"))
    await asyncio.sleep(0.01)

    assert halt.halted and published == []


async def test_a_failed_append_to_the_log_halts_the_session_as_the_event_log() -> None:
    halt = StorageHalt(lambda event: asyncio.sleep(0))
    halt.log_failed(StoreFailed("full"))

    assert halt.halted
    assert halt.message == halting_message(FailedWrite.EVENT_LOG)
    await halt.close()


async def test_a_write_is_skipped_while_the_session_is_halted() -> None:
    halt = StorageHalt(lambda event: asyncio.sleep(0))
    halt.fail(FailedWrite.EVENT_LOG, StoreFailed("first"))
    attempts: list[str] = []

    async def attempt() -> StoreFailed | None:
        attempts.append("tried")
        return None

    await written(halt, FailedWrite.USAGE, attempt)

    assert attempts == []
    await halt.close()


async def test_a_failing_write_halts_the_session_as_that_write() -> None:
    halt = StorageHalt(lambda event: asyncio.sleep(0))

    async def attempt() -> StoreFailed | None:
        return StoreFailed("full")

    await written(halt, FailedWrite.REVIEW_COUNT, attempt)

    assert halt.message == halting_message(FailedWrite.REVIEW_COUNT)
    await halt.close()


async def test_a_write_that_works_leaves_the_session_running() -> None:
    halt = StorageHalt(lambda event: asyncio.sleep(0))
    attempts: list[str] = []

    async def attempt() -> StoreFailed | None:
        attempts.append("tried")
        return None

    await written(halt, FailedWrite.USAGE, attempt)

    assert attempts == ["tried"] and not halt.halted
    await halt.close()


async def test_an_outbox_without_a_log_diverts_and_loses_nothing() -> None:
    outbox = Outbox(lambda: "a")

    assert await outbox.stop_log("a") == ([], None)
    assert await outbox.resume_log() == []


# Resuming


async def halted_session(
    store: FakeStore, fallback: FakeFallback, *scripts: Script
) -> WorkspaceHost:
    """A session over ``store`` whose agents play ``scripts``, halted by its failing appends,
    with three notices published first."""
    store.append_failure = "disk full"
    session, _, _ = session_for(*scripts, store=store, fallback=fallback)
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
    assert (await snapshot_of(session)).storage_halt is None
    assert (await outcome_of(session, RunTasks()))[-1].payload == ACCEPTED
    await session.close()
    assert [envelope.seq for envelope in store.logged] == sorted(
        {envelope.seq for envelope in store.logged}
    )


async def resumed_session(*scripts: Script) -> WorkspaceHost:
    """A session whose agents play ``scripts``, halted and then resumed, with a task waiting."""
    store, fallback = FakeStore(), FakeFallback()
    session = await halted_session(store, fallback, *scripts)
    store.append_failure = None
    assert (await outcome_of(session, ResumeStorage()))[-1].payload == ACCEPTED
    await send(session, AddTask(task=TASK))
    return session


async def worked(session: WorkspaceHost) -> list[str]:
    """The statuses of the session's tasks once its work lane goes idle again."""
    await asyncio.wait_for(events_until(session, idle()), 5)
    return [t.status for t in tasks_of(session).board.tasks]


async def test_a_run_after_resuming_works_the_tasks() -> None:
    session = await resumed_session(claims_done)

    assert (await outcome_of(session, RunTasks()))[-1].payload == ACCEPTED
    assert await worked(session) == ["completed"]
    await session.close()


async def test_a_reviews_wake_after_resuming_starts_the_queued_work() -> None:
    session = await resumed_session(claims_done)
    await turns_of(session).wake()

    assert await worked(session) == ["completed"]
    await session.close()


async def test_resuming_runs_the_review_waiting_on_the_board() -> None:
    store, fallback = FakeStore(), FakeFallback()
    store.append_failure = "disk full"
    session, _, launcher = session_for(
        reviewers=(suggests(),), tree=FakeTree(), review=True, store=store, fallback=fallback
    )
    await publish(session, Notice(message="n0"))
    await halt_error(session)
    tasks_of(session).board.add(TaskSpec(prompt="Review it.", role=REVIEWER))
    store.append_failure = None

    assert (await outcome_of(session, ResumeStorage()))[-1].payload == ACCEPTED
    await events_until(session, lambda event: isinstance(event, AgentExited))
    assert [launch.agent.role for launch in launcher.launches] == [REVIEWER]
    await session.close()


async def test_a_resume_that_fails_to_lift_the_halt_starts_no_review() -> None:
    store, fallback = FakeStore(), FakeFallback()
    store.append_failure = "disk full"
    session, _, launcher = session_for(
        reviewers=(suggests(),), tree=FakeTree(), review=True, store=store, fallback=fallback
    )
    await publish(session, Notice(message="n0"))
    await halt_error(session)
    tasks_of(session).board.add(TaskSpec(prompt="Review it.", role=REVIEWER))
    store.append_failure = "still full"

    refusal = (await outcome_of(session, ResumeStorage()))[-1].payload
    assert isinstance(refusal, CommandRefused) and refusal.code == STORAGE_FAILED
    await settled()
    assert not lane_of(session).running
    assert launcher.launches == []
    await session.close()


async def test_a_close_during_a_resumes_retry_starts_no_review() -> None:
    store, fallback = FakeStore(), FakeFallback()
    store.append_failure = "disk full"
    session, _, launcher = session_for(
        reviewers=(suggests(),), tree=FakeTree(), review=True, store=store, fallback=fallback
    )
    await publish(session, Notice(message="n0"))
    await halt_error(session)
    tasks_of(session).board.add(TaskSpec(prompt="Review it.", role=REVIEWER))
    store.append_failure = None
    retry = BlockedRetry(asyncio.Event(), asyncio.Event())
    install_retry(session, retry)

    resuming = asyncio.create_task(send(session, ResumeStorage()))
    await retry.started.wait()
    await session.close()
    retry.release.set()
    await resuming
    await settled()

    assert not halt_of(session).halted
    assert not lane_of(session).running
    assert launcher.launches == []


async def test_resuming_does_not_run_the_startup_check_the_halt_cancelled() -> None:
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
    store.append_failure = None

    assert (await outcome_of(session, ResumeStorage()))[-1].payload == ACCEPTED
    await settled()
    assert not startup.running and startup.agent is None
    assert steps.runs == ["Lint"]  # Only the run the halt cancelled.
    await session.close()


async def test_the_startup_check_does_not_start_on_a_halted_session() -> None:
    steps = Steps(True)
    session, _, _ = session_for(
        verification=checked(steps), tree=FakeTree(), check_at_startup=True, store=FakeStore()
    )
    halt_of(session).fail(FailedWrite.EVENT_LOG, StoreFailed("disk full"))
    session.start_background()
    startup = startup_of(session)
    await session.close()

    assert startup is not None and not startup.running and startup.panel is None
    assert steps.runs == []


async def halted_check(steps: Steps) -> WorkspaceHost:
    """A session that checks ``steps`` at startup, halted, its halting done, before anything
    started."""
    session, _, _ = session_for(
        verification=checked(steps), tree=FakeTree(), check_at_startup=True, store=FakeStore()
    )
    halt_of(session).fail(FailedWrite.EVENT_LOG, StoreFailed("disk full"))
    await settled()
    return session


async def still_full() -> tuple[FailedWrite, StoreFailed]:
    return FailedWrite.EVENT_LOG, StoreFailed("still full")


async def test_the_startup_check_the_halt_kept_from_starting_runs_once_resumed() -> None:
    steps = Steps(True, True)
    session = await halted_check(steps)
    session.start_background()

    await resume_storage(session)
    await events_until(session, agent_exited("startup-check"))
    startup = startup_of(session)
    assert startup is not None and startup.panel is not None
    assert steps.runs == ["Lint", "Unit tests"]
    await resume_storage(session)
    await settled()
    assert steps.runs == ["Lint", "Unit tests"]
    await session.close()


async def test_a_resume_that_leaves_the_session_halted_starts_no_startup_check() -> None:
    steps = Steps(True)
    session = await halted_check(steps)
    session.start_background()
    install_retry(session, still_full)

    with pytest.raises(Refused):
        await resume_storage(session)
    await settled()
    startup = startup_of(session)
    assert startup is not None and not startup.running and startup.panel is None
    assert steps.runs == []
    await session.close()


async def test_resuming_does_not_start_the_startup_check_again_while_it_runs() -> None:
    steps = Steps(True, True)
    steps.gate = asyncio.Event()
    session, _, _ = session_for(
        verification=checked(steps), tree=FakeTree(), check_at_startup=True, store=FakeStore()
    )
    session.start_background()
    while steps.runs == []:
        await asyncio.sleep(0.01)

    await resume_storage(session)
    steps.gate.set()
    await events_until(session, agent_exited("startup-check"))
    await settled()
    assert steps.runs == ["Lint", "Unit tests"]
    await session.close()


async def test_a_resume_before_the_background_starts_does_not_start_the_startup_check() -> None:
    steps = Steps(True)
    session = await halted_check(steps)

    await resume_storage(session)
    await settled()
    startup = startup_of(session)
    assert startup is not None and not startup.running and startup.panel is None
    assert steps.runs == []
    await session.close()


async def test_resuming_logs_a_gap_for_events_that_reached_no_fallback() -> None:
    store, fallback = FakeStore(), FakeFallback()
    fallback.broken = True
    session = await halted_session(store, fallback)
    store.append_failure = None
    lost = [envelope.seq for envelope in (await session.outbox.transcript("session-1")).events]

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
    assert (await snapshot_of(session)).storage_halt == refusal.message
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


async def test_a_session_whose_lock_could_not_be_taken_is_saved_on_resuming() -> None:
    store = FakeStore()
    store.lock_failure = "Couldn't lock the session x.lock: disk full"
    session, _, _ = session_for(store=store, fallback=FakeFallback())
    await send(session, AddTask(task=TASK))
    error = await halt_error(session)

    assert "saving the session record" in error.message
    assert "Couldn't lock the session x.lock: disk full" in error.message
    store.lock_failure = None
    assert (await outcome_of(session, ResumeStorage()))[-1].payload == ACCEPTED
    assert store.locked == {"session-1"}
    assert [record.tasks[0].task for record in store.saved] == [TASK]
    await session.close()
    assert store.released == ["session-1"]


async def test_resuming_while_the_review_count_still_fails_names_it() -> None:
    store = FakeStore()
    store.count_failure = "count still full"
    session, _, _ = session_for(store=store, fallback=FakeFallback())
    await review_count_of(session).load()
    await review_count_of(session).save(3)
    await halt_error(session)

    refusal = (await outcome_of(session, ResumeStorage()))[-1].payload
    assert isinstance(refusal, CommandRefused) and "saving the review count" in refusal.message
    await session.close()


async def test_resuming_while_the_turns_usage_still_fails_names_it() -> None:
    ledger = FakeLedger()
    ledger.failure = "usage still full"
    session, _, launcher = session_for(store=FakeStore(), fallback=FakeFallback(), ledger=ledger)
    launcher.planner_turns = [chats]
    launcher.planner_usages = [Usage(input_tokens=10)]
    await send(session, SendMessage(agent=PLANNER, text="Plan it."))
    error = await halt_error(session)
    assert "saving the turns' usage" in error.message

    refusal = (await outcome_of(session, ResumeStorage()))[-1].payload
    assert isinstance(refusal, CommandRefused) and "saving the turns' usage" in refusal.message
    ledger.failure = None
    assert (await outcome_of(session, ResumeStorage()))[-1].payload == ACCEPTED
    assert [row.usage for row in ledger.rows] == [Usage(input_tokens=10)]
    await session.close()


async def test_resuming_a_session_that_isnt_halted_is_accepted_and_does_nothing() -> None:
    store = FakeStore()
    session, _, _ = session_for(store=store, fallback=FakeFallback())

    assert (await outcome_of(session, ResumeStorage()))[-1].payload == ACCEPTED
    await session.close()


async def test_resuming_while_the_halt_is_still_under_way_is_refused() -> None:
    halt = StorageHalt(lambda event: asyncio.sleep(0))
    halt.fail(FailedWrite.EVENT_LOG, StoreFailed("full"))

    with pytest.raises(Refused) as refusal:
        await halt.resume()
    assert str(refusal.value) == halting_message(FailedWrite.EVENT_LOG)
    await halt.close()


async def test_a_resumed_halt_halts_again_on_the_next_failure() -> None:
    published: list[Event] = []

    async def record(event: Event) -> None:
        published.append(event)

    halt = StorageHalt(record, HaltHooks())
    halt.fail(FailedWrite.EVENT_LOG, StoreFailed("first"))
    while halt.message == halting_message(FailedWrite.EVENT_LOG):
        await asyncio.sleep(0)
    await asyncio.sleep(0.01)
    await halt.resume()
    assert not halt.halted
    halt.fail(FailedWrite.EVENT_LOG, StoreFailed("second"))
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
    assert (await snapshot_of(session)).storage_halt == error.message
    assert (await outcome_of(session, RunTasks()))[-1].payload == refused(
        STORAGE_FAILED, error.message
    )
    await session.close()


async def test_a_halt_lifted_despite_failures_halts_again_on_the_first() -> None:
    published: list[Event] = []

    async def record(event: Event) -> None:
        published.append(event)

    async def retry() -> tuple[FailedWrite, StoreFailed] | None:
        halt.fail(FailedWrite.EVENT_LOG, StoreFailed("again"))
        halt.fail(FailedWrite.SESSION_RECORD, StoreFailed("later"))
        return None

    halt = StorageHalt(record, HaltHooks(retry=retry))
    halt.fail(FailedWrite.EVENT_LOG, StoreFailed("first"))
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


@dataclass
class BlockedRetry:
    """A retry that waits for :attr:`release`, counting how often it ran."""

    started: asyncio.Event
    release: asyncio.Event
    runs: int = 0

    async def __call__(self) -> tuple[FailedWrite, StoreFailed] | None:
        self.runs += 1
        self.started.set()
        await self.release.wait()
        return None


async def halted_on_retry(
    record: Callable[[Event], Awaitable[None]],
) -> tuple[StorageHalt, BlockedRetry]:
    """A halt, its halting done, whose retry blocks until released."""
    retry = BlockedRetry(asyncio.Event(), asyncio.Event())
    halt = StorageHalt(record, HaltHooks(retry=retry))
    halt.fail(FailedWrite.EVENT_LOG, StoreFailed("first"))
    while halt.message == halting_message(FailedWrite.EVENT_LOG):
        await asyncio.sleep(0)
    await asyncio.sleep(0.01)
    return halt, retry


async def test_a_resume_while_one_is_under_way_is_refused() -> None:
    async def record(event: Event) -> None:
        pass

    halt, retry = await halted_on_retry(record)
    first = asyncio.create_task(halt.resume())
    await retry.started.wait()
    with pytest.raises(Refused) as refusal:
        await asyncio.wait_for(halt.resume(), 1)
    assert refusal.value.code == STORAGE_FAILED
    retry.release.set()
    await first
    assert retry.runs == 1
    assert not halt.halted
    await halt.close()


async def test_a_failure_during_a_resume_refused_a_second_time_halts_again() -> None:
    published: list[Event] = []

    async def record(event: Event) -> None:
        published.append(event)

    halt, retry = await halted_on_retry(record)
    first = asyncio.create_task(halt.resume())
    await retry.started.wait()
    with pytest.raises(Refused):
        await asyncio.wait_for(halt.resume(), 1)
    halt.fail(FailedWrite.EVENT_LOG, StoreFailed("again"))
    retry.release.set()
    await first
    assert halt.halted
    while halt.message == halting_message(FailedWrite.EVENT_LOG):
        await asyncio.sleep(0)
    await halt.close()

    errors = [event.message for event in published if isinstance(event, ErrorOccurred)]
    [_, again] = errors
    assert "again" in again


@dataclass
class BlockedDivert:
    """A divert that, after the halt's own, waits for :attr:`release`."""

    started: asyncio.Event
    release: asyncio.Event
    runs: int = 0

    async def __call__(self) -> tuple[Sequence[EventEnvelope], str | None]:
        self.runs += 1
        if self.runs > 1:
            self.started.set()
            await self.release.wait()
        return (), None


async def test_a_resume_while_one_is_explaining_its_failed_retry_is_refused() -> None:
    retries = 0

    async def retry() -> tuple[FailedWrite, StoreFailed] | None:
        nonlocal retries
        retries += 1
        return FailedWrite.SESSION_RECORD, StoreFailed("still full")

    divert = BlockedDivert(asyncio.Event(), asyncio.Event())
    halt = StorageHalt(lambda event: asyncio.sleep(0), HaltHooks(divert=divert, retry=retry))
    halt.fail(FailedWrite.EVENT_LOG, StoreFailed("first"))
    while halt.message == halting_message(FailedWrite.EVENT_LOG):
        await asyncio.sleep(0)
    await asyncio.sleep(0.01)
    first = asyncio.create_task(halt.resume())
    await divert.started.wait()
    with pytest.raises(Refused) as second:
        await asyncio.wait_for(halt.resume(), 1)
    assert second.value.code == STORAGE_FAILED
    assert retries == 1

    divert.release.set()
    with pytest.raises(Refused) as refusal:
        await first
    assert "still full" in str(refusal.value)
    assert halt.halted
    assert halt.message == str(refusal.value)
    with pytest.raises(Refused):
        await halt.resume()
    assert retries == 2
    await halt.close()
