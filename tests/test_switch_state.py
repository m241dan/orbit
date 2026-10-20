"""Switching sessions is refused exactly while the open session's published state is running,
whatever runs in it."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest

from orbit.agent import AgentSession
from orbit.agent.domain.sessions import SessionRecord
from orbit.protocol import (
    BUSY,
    PLANNER,
    ActivateSession,
    AddTask,
    AgentStarted,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    Event,
    RunTasks,
    SendMessage,
    SessionState,
    SessionStateChanged,
    TaskSpec,
    TurnEndReason,
)
from tests.agent_fakes import (
    Call,
    FakeStore,
    FakeTree,
    Steps,
    Stop,
    all_of,
    frontend,
    greet,
    idle,
    send,
    session_for,
)
from tests.test_planner_lane import gated_planner
from tests.test_startup_check import checked
from tests.test_task_runs import claims_done

OPEN = "session-1"
"""The id of the session :func:`session_for` opens."""

TASK = TaskSpec(prompt="Add a flag.")

RUNNING = Stop("running", lambda e: isinstance(e, SessionStateChanged) and e.state == "running")
IDLE = Stop("idle", lambda e: isinstance(e, SessionStateChanged) and e.state == "idle")

Busy = Callable[[FakeStore, asyncio.Event], Awaitable[tuple[AgentSession, Stop]]]
"""Makes a session saved in a store busy with one activity until a gate opens; returns it and
the event at which that activity is the one in progress."""


class Watch:
    """The events reaching ``session``'s frontend, and the open session's state as last
    published among them."""

    def __init__(self, session: AgentSession) -> None:
        self._stream = frontend(session).events()
        self.state: SessionState | None = None

    async def until(self, done: Callable[[Event], bool]) -> Event:
        """The first event from here on that's ``done``."""
        while True:
            event = (await asyncio.wait_for(anext(self._stream), 10)).payload
            if isinstance(event, SessionStateChanged) and event.session == OPEN:
                self.state = event.state
            if done(event):
                return event


def gated_work(gate: asyncio.Event) -> Callable[[Call], Awaitable[TurnEndReason]]:
    async def works(call: Call) -> TurnEndReason:
        await call("get_task")
        await gate.wait()
        await call("complete_task", summary="done")
        return "completed"

    return works


async def running_tasks(store: FakeStore, gate: asyncio.Event) -> tuple[AgentSession, Stop]:
    session, _, _ = session_for(gated_work(gate), store=store)
    await send(session, AddTask(task=TASK))
    await send(session, RunTasks())
    return session, RUNNING


async def reviewing(store: FakeStore, gate: asyncio.Event) -> tuple[AgentSession, Stop]:
    reviewer = gated_work(gate)
    session, _, _ = session_for(
        claims_done, reviewers=(reviewer,), tree=FakeTree(), review=True, store=store
    )
    await send(session, AddTask(task=TASK))
    await send(session, RunTasks())
    started = Stop("reviewer", lambda e: isinstance(e, AgentStarted) and e.role == "reviewer")
    return session, all_of(started, idle("work"))


async def planning(store: FakeStore, gate: asyncio.Event) -> tuple[AgentSession, Stop]:
    session, _, launcher = session_for(store=store)
    launcher.planner_turns = [gated_planner(gate)]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    return session, RUNNING


async def checking(store: FakeStore, gate: asyncio.Event) -> tuple[AgentSession, Stop]:
    steps = Steps(True, True)
    steps.gate = gate
    session, _, _ = session_for(verification=checked(steps), check_at_startup=True, store=store)
    await greet(session)
    return session, RUNNING


async def activating(session: AgentSession, watch: Watch, command_id: str) -> Event:
    """The answer to activating the saved session ``earlier`` as ``command_id``."""
    command = ActivateSession(session="earlier")
    await frontend(session).send(CommandEnvelope(command_id=command_id, payload=command))
    return await watch.until(
        lambda e: isinstance(e, CommandAccepted | CommandRefused) and e.command_id == command_id
    )


@pytest.mark.parametrize("busy", [running_tasks, reviewing, planning, checking])
async def test_activating_is_refused_as_busy_exactly_while_the_state_is_running(
    busy: Busy,
) -> None:
    store, gate = FakeStore(), asyncio.Event()
    when = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    store.saved.append(SessionRecord(id="earlier", name="Earlier", created=when, last_active=when))
    session, ready = await busy(store, gate)
    watch = Watch(session)
    await watch.until(ready)
    refused = await activating(session, watch, "while-running")
    while_running = watch.state
    gate.set()
    await watch.until(IDLE)
    accepted = await activating(session, watch, "once-idle")
    once_idle = watch.state
    await session.close()

    assert isinstance(refused, CommandRefused) and refused.code == BUSY
    assert isinstance(accepted, CommandAccepted)
    assert (while_running, once_idle) == ("running", "idle")
