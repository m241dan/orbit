"""A run stopped at Claude's usage limit, or at a fix's error, and started again by itself once
the limit resets."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from orbit.agent import WorkspaceHost
from orbit.agent.application.limit_pause import LimitPause
from orbit.agent.application.ports import StoreFailed, UsageSource
from orbit.agent.application.storage_halt import Failure
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.agent.domain.usage import UsageReport
from orbit.agent.domain.usage_limits import (
    FIX_ERROR_NOTE,
    RESUMING_NOTE,
    WAIT_CANCELLED_NOTE,
    paused_note,
)
from orbit.protocol import (
    CancelTurn,
    Event,
    NewSession,
    Notice,
    ResumeStorage,
    RunTasks,
    TaskSpec,
    TurnEndReason,
    UsageLimit,
)
from tests.agent_fakes import (
    START,
    Call,
    FakeStore,
    FakeTree,
    Script,
    ScriptedLauncher,
    Source,
    Steps,
    board,
    claims_done,
    events_until,
    idle,
    keeps_claiming,
    payloads,
    publish,
    run,
    send,
    session_for,
    statuses,
    verified,
)

LIMIT = "You've hit your session limit · resets 6pm (UTC)"


async def errs(call: Call) -> TurnEndReason:
    return "error"


class Waits:
    """A pause whose clock stands at :data:`START` and whose sleeps last until released."""

    def __init__(self) -> None:
        self.slept: list[float] = []
        self.release = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        await self.release.wait()

    @property
    def pause(self) -> LimitPause:
        return LimitPause(now=lambda: START, sleep=self.sleep)


def limited(
    *scripts: Script,
    waits: Waits,
    store: FakeStore | None = None,
    usage: UsageSource | None = None,
) -> tuple[WorkspaceHost, ScriptedLauncher]:
    """A session whose first agent hits the usage limit, then plays ``scripts``, with usage
    from ``usage``, if given."""
    session, _, launcher = session_for(errs, *scripts, pause=waits.pause, store=store, usage=usage)
    launcher.turn_errors.append(LIMIT)
    return session, launcher


async def drained(session: WorkspaceHost) -> list[Event]:
    """The session's events until none comes for a moment."""
    events: list[Event] = []
    stream = payloads(session)
    try:
        while True:
            events.append(await asyncio.wait_for(anext(stream), 0.05))
    except TimeoutError:
        return events


def notes(events: list[Event]) -> list[str]:
    return [e.message for e in events if isinstance(e, Notice)]


async def resumed(session: WorkspaceHost, waits: Waits) -> list[Event]:
    """Release the wait, and the session's events until its run goes idle again."""
    waits.release.set()
    return await events_until(session, idle())


async def test_a_usage_limit_stops_the_run_and_says_when_it_starts_again() -> None:
    waits = Waits()
    session, launcher = limited(claims_done, waits=waits)
    events = await asyncio.wait_for(run(session, TaskSpec(prompt="a"), TaskSpec(prompt="b")), 5)

    assert [launch.agent.task_id for launch in launcher.launches] == ["1"]
    assert statuses(events) == ["error", "pending"]
    assert board(events)[0].reason_detail == LIMIT
    assert notes(events)[-1] == paused_note(START + timedelta(minutes=15))
    await session.close()


async def test_the_run_starts_again_by_itself_at_the_reset_the_usage_shows() -> None:
    waits = Waits()
    resets = START + timedelta(hours=2)
    used_up = UsageReport((UsageLimit(label="Session", percent=100, resets_at=resets),))
    session, launcher = limited(claims_done, claims_done, waits=waits, usage=Source(used_up))
    first = await asyncio.wait_for(run(session, TaskSpec(prompt="a"), TaskSpec(prompt="b")), 5)
    assert notes(first)[-1] == paused_note(resets + timedelta(minutes=1))

    second = await asyncio.wait_for(resumed(session, waits), 5)
    await session.close()

    assert waits.slept == [(resets + timedelta(minutes=1) - START).total_seconds()]
    assert RESUMING_NOTE in notes(second)
    assert [launch.agent.task_id for launch in launcher.launches] == ["1", "1", "2"]
    assert statuses(second) == ["completed", "completed"]


async def test_stopping_a_waiting_run_cancels_its_restart() -> None:
    waits = Waits()
    session, launcher = limited(waits=waits)
    await asyncio.wait_for(run(session), 5)
    await send(session, CancelTurn())
    waits.release.set()
    events = await drained(session)
    await session.close()

    assert WAIT_CANCELLED_NOTE in notes(events)
    assert RESUMING_NOTE not in notes(events)
    assert len(launcher.launches) == 1


async def test_running_while_waiting_starts_at_once_and_drops_the_wait() -> None:
    waits = Waits()
    session, launcher = limited(claims_done, waits=waits)
    await asyncio.wait_for(run(session), 5)
    await send(session, RunTasks())
    events = await asyncio.wait_for(events_until(session, idle()), 5)
    waits.release.set()
    events += await drained(session)
    await session.close()

    assert statuses(events) == ["completed"]
    assert RESUMING_NOTE not in notes(events)
    assert len(launcher.launches) == 2


async def test_switching_sessions_drops_the_wait() -> None:
    waits = Waits()
    session, launcher = limited(waits=waits, store=FakeStore())
    await asyncio.wait_for(run(session), 5)
    await send(session, NewSession(name="other"))
    waits.release.set()
    events = await drained(session)
    await session.close()

    assert len(launcher.launches) == 1
    assert RESUMING_NOTE not in notes(events)


async def test_closing_while_waiting_ends_the_wait() -> None:
    waits = Waits()
    session, _ = limited(waits=waits)
    await asyncio.wait_for(run(session), 5)
    await asyncio.wait_for(session.close(), 5)

    assert not waits.release.is_set()


class SlowWaits(Waits):
    """Waits whose sleep, once cancelled, keeps unwinding until cancelled again; ``unwinding``
    is set once it is."""

    def __init__(self) -> None:
        super().__init__()
        self.unwinding = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        try:
            await super().sleep(seconds)
        except asyncio.CancelledError:
            self.unwinding.set()
            await asyncio.Event().wait()


async def test_a_close_cancelled_while_the_wait_ends_is_cancelled_once_every_part_stops() -> None:
    waits = SlowWaits()
    session, launcher = limited(waits=waits)
    await asyncio.wait_for(run(session), 5)
    closing = asyncio.create_task(session.close())
    await asyncio.wait_for(waits.unwinding.wait(), 5)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, 5)

    assert len(launcher.launches) == 1


async def halt(session: WorkspaceHost, store: FakeStore) -> list[Event]:
    """Halt the session with a failing append to ``store``, and its events until it settles."""
    store.append_failure = "disk full"
    await publish(session, Notice(message="n"))
    return await drained(session)


async def resume(session: WorkspaceHost, store: FakeStore) -> list[Event]:
    """Lift the session's halt, appends to ``store`` working again, and its events until it
    settles."""
    store.append_failure = None
    await send(session, ResumeStorage())
    return await drained(session)


async def test_a_wait_kept_through_a_halt_starts_the_run_at_the_reset() -> None:
    waits, store = Waits(), FakeStore()
    session, launcher = limited(claims_done, waits=waits, store=store)
    await asyncio.wait_for(run(session), 5)
    await halt(session, store)
    await resume(session, store)
    events = await asyncio.wait_for(resumed(session, waits), 5)
    await session.close()

    assert RESUMING_NOTE in notes(events)
    assert statuses(events) == ["completed"]
    assert len(launcher.launches) == 2


async def test_a_wait_ending_while_halted_starts_the_run_once_the_halt_lifts() -> None:
    waits, store = Waits(), FakeStore()
    session, launcher = limited(claims_done, waits=waits, store=store)
    await asyncio.wait_for(run(session), 5)
    await halt(session, store)
    waits.release.set()
    halted = await drained(session)
    assert RESUMING_NOTE not in notes(halted)
    assert len(launcher.launches) == 1

    store.append_failure = None
    await send(session, ResumeStorage())
    events = await asyncio.wait_for(events_until(session, idle()), 5)
    await session.close()

    assert RESUMING_NOTE in notes(events)
    assert statuses(events) == ["completed"]
    assert len(launcher.launches) == 2


def fails_as_lifted(session: WorkspaceHost) -> None:
    """Have a write fail while the session's next resume retries the writes that failed."""
    retry = session._halt._retry

    async def failing() -> Failure | None:
        session._halt.fail(FailedWrite.EVENT_LOG, StoreFailed("full again"))
        return await retry()

    session._halt._retry = failing


async def test_a_wait_ended_while_halted_starts_nothing_while_a_resume_halts_again() -> None:
    waits, store = Waits(), FakeStore()
    session, launcher = limited(claims_done, waits=waits, store=store)
    await asyncio.wait_for(run(session), 5)
    await halt(session, store)
    waits.release.set()
    await drained(session)
    fails_as_lifted(session)
    events = await resume(session, store)

    assert session.storage_halted
    assert RESUMING_NOTE not in notes(events)
    assert len(launcher.launches) == 1
    await session.close()


async def test_stopping_a_wait_kept_through_a_halt_cancels_its_restart() -> None:
    waits, store = Waits(), FakeStore()
    session, launcher = limited(waits=waits, store=store)
    await asyncio.wait_for(run(session), 5)
    await halt(session, store)
    await resume(session, store)
    await send(session, CancelTurn())
    waits.release.set()
    events = await drained(session)
    await session.close()

    assert WAIT_CANCELLED_NOTE in notes(events)
    assert RESUMING_NOTE not in notes(events)
    assert len(launcher.launches) == 1


def test_a_pause_tells_the_time_in_utc_by_default() -> None:
    before = datetime.now(UTC)
    now = LimitPause().now()
    assert now.tzinfo is UTC and before <= now <= datetime.now(UTC)


async def test_a_fix_that_ends_in_error_stops_the_run_before_later_tasks() -> None:
    session, _, launcher = session_for(
        keeps_claiming,
        errs,
        verification=verified(Steps(False, False)),
        limits=Limits(max_attempts=1),
    )
    events = await asyncio.wait_for(run(session, TaskSpec(prompt="one"), TaskSpec(prompt="two")), 5)
    await session.close()

    one, fix, two = board(events)
    assert fix.fixes == one.id
    assert (one.status, fix.status, two.status) == ("unverified", "error", "pending")
    assert [launch.agent.task_id for launch in launcher.launches] == [one.id, fix.id]
    assert FIX_ERROR_NOTE in notes(events)


async def test_the_next_run_starts_with_a_fix_that_stopped_one_on_the_tree_holding_its_work() -> (
    None
):
    tree = FakeTree()
    session, _, launcher = session_for(
        errs,
        keeps_claiming,
        errs,
        errs,
        verification=verified(Steps(False, False)),
        limits=Limits(max_attempts=1),
        tree=tree,
    )
    launcher.turn_errors.append("boom")
    first = await asyncio.wait_for(run(session, TaskSpec(prompt="zero"), TaskSpec(prompt="one")), 5)
    zero, one, fix = board(first)
    assert (zero.status, one.status, fix.status) == ("error", "unverified", "error")

    await send(session, RunTasks())
    await asyncio.wait_for(events_until(session, idle()), 5)
    await session.close()

    assert [launch.agent.task_id for launch in launcher.launches] == [
        zero.id,
        one.id,
        fix.id,
        fix.id,
    ]
    assert tree.prepared == [False, False, True, True]


async def test_the_next_run_starts_with_the_task_at_the_limit_then_keeps_board_order() -> None:
    waits = Waits()
    session, _, launcher = session_for(
        errs, errs, claims_done, claims_done, pause=waits.pause, tree=FakeTree()
    )
    launcher.turn_errors.extend(["boom", LIMIT])
    first = await asyncio.wait_for(run(session, TaskSpec(prompt="a"), TaskSpec(prompt="b")), 5)
    a, b = board(first)
    assert (a.status, b.status) == ("error", "error")

    await send(session, RunTasks())
    events = await asyncio.wait_for(events_until(session, idle()), 5)
    await session.close()

    assert [launch.agent.task_id for launch in launcher.launches] == [a.id, b.id, b.id, a.id]
    assert statuses(events) == ["completed", "completed"]


def forgets_where_it_stopped(session: WorkspaceHost) -> None:
    """Have the session's work lane forget the task its last run stopped at, as switching
    sessions away and back does, leaving the board to say what runs first."""
    lane = session._work
    assert lane is not None
    lane.reset()


async def test_a_fix_in_error_runs_before_an_earlier_task_in_error_on_a_tree_with_its_work() -> (
    None
):
    tree = FakeTree()
    session, _, launcher = session_for(
        errs,
        keeps_claiming,
        errs,
        errs,
        verification=verified(Steps(False, False)),
        limits=Limits(max_attempts=1),
        tree=tree,
    )
    launcher.turn_errors.append("boom")
    first = await asyncio.wait_for(run(session, TaskSpec(prompt="a"), TaskSpec(prompt="b")), 5)
    a, b, fix = board(first)
    assert (a.status, b.status, fix.status) == ("error", "unverified", "error")

    forgets_where_it_stopped(session)
    await send(session, RunTasks())
    await asyncio.wait_for(events_until(session, idle()), 5)
    await session.close()

    assert [launch.agent.task_id for launch in launcher.launches] == [a.id, b.id, fix.id, fix.id]
    assert tree.prepared == [False, False, True, True]


def hits_the_limit(launcher: ScriptedLauncher) -> Script:
    """A script whose turn ends in error as its agent hits the usage limit."""

    async def script(call: Call) -> TurnEndReason:
        launcher.turn_errors.append(LIMIT)
        return "error"

    return script


async def test_a_fix_at_the_limit_runs_before_an_earlier_task_in_error_on_starting_again() -> None:
    waits, tree = Waits(), FakeTree()
    session, _, launcher = session_for(
        errs,
        keeps_claiming,
        verification=verified(Steps(False, False)),
        limits=Limits(max_attempts=1),
        pause=waits.pause,
        tree=tree,
    )
    launcher.scripts.extend([hits_the_limit(launcher), errs])
    launcher.turn_errors.append("boom")
    first = await asyncio.wait_for(run(session, TaskSpec(prompt="a"), TaskSpec(prompt="b")), 5)
    a, b, fix = board(first)
    assert (a.status, b.status, fix.status) == ("error", "unverified", "error")
    assert notes(first)[-1] == paused_note(START + timedelta(minutes=15))

    forgets_where_it_stopped(session)
    await send(session, RunTasks())
    await asyncio.wait_for(events_until(session, idle()), 5)
    await session.close()

    assert [launch.agent.task_id for launch in launcher.launches] == [a.id, b.id, fix.id, fix.id]
    assert tree.prepared == [False, False, True, True]
