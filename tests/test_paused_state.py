"""A run waiting to start again as the session's state shows it: until when and why, from the
usage limit or an outage stopping it until it starts again, or its wait is cancelled, dropped
or replaced by a run the user starts."""

import asyncio
from datetime import datetime, timedelta

from orbit.agent import WorkspaceHost
from orbit.agent.application.limit_pause import LimitPause
from orbit.agent.domain.runtimes import CLAUDE
from orbit.protocol import (
    PAUSED_FOR_API_UNAVAILABLE,
    PAUSED_FOR_USAGE_LIMIT,
    AddTask,
    CancelTurn,
    Catalog,
    Event,
    NewSession,
    RemoveTask,
    RunTasks,
    SessionStateChanged,
    SessionSummary,
    TaskSpec,
    TurnEndReason,
)
from tests.agent_fakes import START, Call, FakeStore, claims_done, send, session_for
from tests.connection_helpers import joined, received, until

LIMIT = "You've hit your session limit · resets 6pm (UTC)"
OVERLOADED = 'API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}'
TASK = TaskSpec(prompt="Add a flag.")


async def errs(call: Call) -> TurnEndReason:
    return "error"


class Waits:
    """A pause whose clock stands at :data:`START` and whose sleeps last until released."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        await self.release.wait()

    @property
    def pause(self) -> LimitPause:
        return LimitPause(now=lambda: START, sleep=self.sleep)


def stopped_by(error: str, waits: Waits, store: FakeStore | None = None) -> WorkspaceHost:
    """A session saved in ``store``, if given, whose first agent ends in ``error`` and whose
    next completes its task."""
    session, _, launcher = session_for(errs, claims_done, pause=waits.pause, store=store)
    launcher.turn_errors.append(error)
    return session


def changes(events: list[Event]) -> list[SessionStateChanged]:
    """The state changes among ``events``."""
    return [e for e in events if isinstance(e, SessionStateChanged)]


def changed(event: Event) -> bool:
    return isinstance(event, SessionStateChanged)


def paused_idle(event: Event) -> bool:
    """Whether ``event`` says the open session is idle, its run paused."""
    return (
        isinstance(event, SessionStateChanged)
        and event.state == "idle"
        and event.paused_until is not None
    )


def running_now(event: Event) -> bool:
    """Whether ``event`` says the open session is running."""
    return isinstance(event, SessionStateChanged) and event.state == "running"


def pause_of(
    state: SessionStateChanged | SessionSummary,
) -> tuple[datetime | None, str | None, str | None]:
    """Until when, why and on which runtime ``state`` says its session's run is paused."""
    return state.paused_until, state.paused_for, state.paused_runtime


async def stopped(session: WorkspaceHost) -> SessionStateChanged:
    """Run a task in ``session``, and return the state saying it's idle, its run paused."""
    await send(session, AddTask(task=TASK))
    await send(session, RunTasks())
    events = await until(session, paused_idle)
    return changes(events)[-1]


async def next_change(session: WorkspaceHost) -> SessionStateChanged:
    """The next state change ``session`` publishes."""
    return changes(await until(session, changed))[-1]


async def test_an_outage_publishes_the_pause_until_the_run_starts_again() -> None:
    waits = Waits()
    session = stopped_by(OVERLOADED, waits)
    paused = await stopped(session)
    waits.release.set()
    events = await until(session, running_now)
    await session.close()

    assert pause_of(paused) == (START + timedelta(minutes=2), PAUSED_FOR_API_UNAVAILABLE, CLAUDE)
    assert pause_of(changes(events)[0]) == (None, None, None)


async def test_a_usage_limit_publishes_the_pause_for_the_limit() -> None:
    session = stopped_by(LIMIT, Waits())
    paused = await stopped(session)
    await session.close()

    assert pause_of(paused) == (START + timedelta(minutes=15), PAUSED_FOR_USAGE_LIMIT, CLAUDE)


async def test_cancelling_the_wait_clears_the_pause() -> None:
    session = stopped_by(LIMIT, Waits())
    await stopped(session)
    await send(session, CancelTurn())
    cleared = await next_change(session)
    await session.close()

    assert pause_of(cleared) == (None, None, None)


async def test_a_run_the_user_starts_during_the_wait_clears_the_pause() -> None:
    session = stopped_by(OVERLOADED, Waits())
    await stopped(session)
    await send(session, RunTasks())
    cleared = await next_change(session)
    await session.close()

    assert pause_of(cleared) == (None, None, None)


async def test_a_wait_ending_with_no_work_left_clears_the_pause() -> None:
    waits = Waits()
    session = stopped_by(LIMIT, waits)
    await stopped(session)
    await send(session, RemoveTask(task_id="1"))
    waits.release.set()
    cleared = await next_change(session)
    await session.close()

    assert pause_of(cleared) == (None, None, None)


async def test_switching_sessions_clears_the_pause() -> None:
    session = stopped_by(LIMIT, Waits(), FakeStore())
    await stopped(session)
    await send(session, NewSession(name="other"))
    events = await received(session)
    await session.close()

    assert changes(events)
    assert all(pause_of(change) == (None, None, None) for change in changes(events))


async def test_a_joining_connections_catalog_carries_the_pause() -> None:
    session = stopped_by(OVERLOADED, Waits())
    await stopped(session)
    second = await joined(session, "second")
    [catalog] = [e for e in await received(second) if isinstance(e, Catalog)]
    await session.close()

    [workspace] = catalog.workspaces
    [summary] = workspace.sessions
    assert pause_of(summary) == (START + timedelta(minutes=2), PAUSED_FOR_API_UNAVAILABLE, CLAUDE)
