"""A run stopped as Claude's API isn't answering, and started again by itself after a backoff
that grows with each outage in a row."""

import asyncio
from datetime import timedelta

from orbit.agent import WorkspaceHost
from orbit.agent.application.limit_pause import LimitPause
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.usage_limits import (
    FIX_ERROR_NOTE,
    OUTAGE_RESUMING_NOTE,
    RESUMING_NOTE,
    WAIT_CANCELLED_NOTE,
    outage_paused_note,
)
from orbit.protocol import (
    CancelTurn,
    Event,
    Notice,
    ResumeStorage,
    RunTasks,
    TaskSpec,
    TurnEndReason,
)
from tests.agent_fakes import (
    START,
    Call,
    FakeStore,
    FakeTree,
    Script,
    ScriptedLauncher,
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
    settled,
    statuses,
    verified,
)
from tests.connection_helpers import quiet

OVERLOADED = 'API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}'


class Clock:
    """A pause whose clock starts at :data:`START` and whose sleeps, noted in ``waited``, last
    until released, then move the clock on to their end."""

    def __init__(self) -> None:
        self.now = START
        self.waited: list[timedelta] = []
        self._released = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self.waited.append(timedelta(seconds=seconds))
        await self._released.wait()
        self._released.clear()
        self.now += timedelta(seconds=seconds)

    def release(self) -> None:
        """End the sleep going on, or the next one."""
        self._released.set()

    @property
    def pause(self) -> LimitPause:
        return LimitPause(now=lambda: self.now, sleep=self.sleep)


def fails(launcher: ScriptedLauncher, message: str) -> Script:
    """A script whose turn ends in error with ``message``, as the CLI reports it."""

    async def script(call: Call) -> TurnEndReason:
        launcher.turn_errors.append(message)
        return "error"

    return script


def hits_an_outage(launcher: ScriptedLauncher) -> Script:
    """A script whose turn ends in error as Claude's API is overloaded."""
    return fails(launcher, OVERLOADED)


def notes(events: list[Event]) -> list[str]:
    return [e.message for e in events if isinstance(e, Notice)]


def task_ids(launcher: ScriptedLauncher) -> list[str]:
    """The tasks the agents launched so far ran, in order."""
    return [launch.agent.task_id for launch in launcher.launches]


async def resumed(session: WorkspaceHost, clock: Clock) -> list[Event]:
    """Release the wait, and the session's events until its run goes idle again."""
    clock.release()
    return await asyncio.wait_for(events_until(session, idle()), 5)


def three_tasks() -> tuple[TaskSpec, ...]:
    return TaskSpec(prompt="a"), TaskSpec(prompt="b"), TaskSpec(prompt="c")


def outage_at_the_second(clock: Clock) -> tuple[WorkspaceHost, ScriptedLauncher]:
    """A session whose second agent hits an outage, and whose agents complete after that."""
    session, _, launcher = session_for(claims_done, pause=clock.pause)
    launcher.scripts.extend([hits_an_outage(launcher), claims_done, claims_done])
    return session, launcher


async def test_an_outage_stops_the_run_at_its_task_and_says_when_it_starts_again() -> None:
    clock = Clock()
    session, launcher = outage_at_the_second(clock)
    events = await asyncio.wait_for(run(session, *three_tasks()), 5)
    await session.close()

    assert task_ids(launcher) == ["1", "2"]
    assert statuses(events) == ["completed", "error", "pending"]
    assert board(events)[1].reason_detail == OVERLOADED
    assert notes(events)[-1] == outage_paused_note(START + timedelta(minutes=2), OVERLOADED)


async def test_the_run_starts_again_with_the_task_at_the_outage_once_the_wait_ends() -> None:
    clock = Clock()
    session, launcher = outage_at_the_second(clock)
    await asyncio.wait_for(run(session, *three_tasks()), 5)
    events = await resumed(session, clock)
    await session.close()

    assert clock.waited == [timedelta(minutes=2)]
    assert OUTAGE_RESUMING_NOTE in notes(events)
    assert RESUMING_NOTE not in notes(events)
    assert task_ids(launcher) == ["1", "2", "2", "3"]
    assert statuses(events) == ["completed", "completed", "completed"]


async def test_outages_in_a_row_wait_longer_until_an_agent_finishes_its_run() -> None:
    clock = Clock()
    session, _, launcher = session_for(pause=clock.pause)
    launcher.scripts.extend(
        [hits_an_outage(launcher), hits_an_outage(launcher), claims_done, hits_an_outage(launcher)]
    )
    first = await asyncio.wait_for(run(session, TaskSpec(prompt="a"), TaskSpec(prompt="b")), 5)
    second = await resumed(session, clock)
    third = await resumed(session, clock)
    await settled()  # The third wait gets as far as its sleep.
    await session.close()

    assert clock.waited == [timedelta(minutes=2), timedelta(minutes=5), timedelta(minutes=2)]
    assert notes(first)[-1] == outage_paused_note(START + timedelta(minutes=2), OVERLOADED)
    assert notes(second)[-1] == outage_paused_note(START + timedelta(minutes=7), OVERLOADED)
    assert notes(third)[-1] == outage_paused_note(START + timedelta(minutes=9), OVERLOADED)
    assert task_ids(launcher) == ["1", "1", "1", "2"]


async def test_an_agent_failing_another_way_starts_the_backoff_over_too() -> None:
    clock = Clock()
    session, _, launcher = session_for(pause=clock.pause)
    launcher.scripts.extend(
        [hits_an_outage(launcher), fails(launcher, "boom"), hits_an_outage(launcher)]
    )
    await asyncio.wait_for(run(session, TaskSpec(prompt="a"), TaskSpec(prompt="b")), 5)
    events = await resumed(session, clock)
    await settled()  # The second wait gets as far as its sleep.
    await session.close()

    assert clock.waited == [timedelta(minutes=2), timedelta(minutes=2)]
    assert statuses(events) == ["error", "error"]
    assert board(events)[0].reason_detail == "boom"


async def test_stopping_a_run_waiting_out_an_outage_cancels_its_restart() -> None:
    clock = Clock()
    session, _, launcher = session_for(pause=clock.pause)
    launcher.scripts.append(hits_an_outage(launcher))
    await asyncio.wait_for(run(session), 5)
    await send(session, CancelTurn())
    clock.release()
    events = await quiet(payloads(session))
    await session.close()

    assert WAIT_CANCELLED_NOTE in notes(events)
    assert OUTAGE_RESUMING_NOTE not in notes(events)
    assert len(launcher.launches) == 1


async def test_running_while_waiting_out_an_outage_starts_at_once_and_drops_the_wait() -> None:
    clock = Clock()
    session, _, launcher = session_for(claims_done, pause=clock.pause)
    launcher.scripts.insert(0, hits_an_outage(launcher))
    await asyncio.wait_for(run(session), 5)
    await send(session, RunTasks())
    events = await asyncio.wait_for(events_until(session, idle()), 5)
    clock.release()
    events += await quiet(payloads(session))
    await session.close()

    assert statuses(events) == ["completed"]
    assert OUTAGE_RESUMING_NOTE not in notes(events)
    assert len(launcher.launches) == 2


async def test_an_outage_wait_ending_while_halted_starts_the_run_once_the_halt_lifts() -> None:
    clock, store = Clock(), FakeStore()
    session, _, launcher = session_for(claims_done, pause=clock.pause, store=store)
    launcher.scripts.insert(0, hits_an_outage(launcher))
    await asyncio.wait_for(run(session), 5)
    store.append_failure = "disk full"
    await publish(session, Notice(message="n"))
    clock.release()
    halted = await quiet(payloads(session))
    assert OUTAGE_RESUMING_NOTE not in notes(halted)

    store.append_failure = None
    await send(session, ResumeStorage())
    events = await asyncio.wait_for(events_until(session, idle()), 5)
    await session.close()

    assert OUTAGE_RESUMING_NOTE in notes(events)
    assert statuses(events) == ["completed"]
    assert len(launcher.launches) == 2


async def test_a_fix_at_an_outage_starts_again_on_the_tree_holding_its_work() -> None:
    clock, tree = Clock(), FakeTree()
    session, _, launcher = session_for(
        keeps_claiming,
        verification=verified(Steps(False, False)),
        limits=Limits(max_attempts=1),
        pause=clock.pause,
        tree=tree,
    )
    launcher.scripts.extend([hits_an_outage(launcher), hits_an_outage(launcher)])
    events = await asyncio.wait_for(run(session, TaskSpec(prompt="a"), TaskSpec(prompt="b")), 5)
    a, fix, b = board(events)
    assert (a.status, fix.status, b.status) == ("unverified", "error", "pending")
    assert fix.fixes == a.id
    assert FIX_ERROR_NOTE not in notes(events)
    assert notes(events)[-1] == outage_paused_note(START + timedelta(minutes=2), OVERLOADED)

    await resumed(session, clock)
    await session.close()

    assert task_ids(launcher) == [a.id, fix.id, fix.id]
    assert tree.prepared == [False, True, True]
