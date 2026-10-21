"""A run stopped at Claude's usage limit, or at a fix's error, and started again by itself once
the limit resets."""

import asyncio
from datetime import UTC, datetime, timedelta

from orbit.agent import WorkspaceHost
from orbit.agent.application.limit_pause import LimitPause
from orbit.agent.application.ports import UsageSource
from orbit.agent.domain.agent_task import Limits
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
