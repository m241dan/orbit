"""Switching sessions is refused exactly while the open session's published state is running,
whatever runs in it."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest

from orbit.agent import WorkspaceHost
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
    TurnEndReason,
)
from tests.agent_fakes import (
    Call,
    FakeStore,
    FakeTree,
    Steps,
    Stop,
    all_of,
    checked,
    claims_done,
    frontend,
    gated_planner,
    greet,
    idle,
    send,
    session_for,
)
from tests.server_fakes import TASK

OPEN = "session-1"
"""The id of the session :func:`session_for` opens."""


RUNNING = Stop("running", lambda e: isinstance(e, SessionStateChanged) and e.state == "running")
IDLE = Stop("idle", lambda e: isinstance(e, SessionStateChanged) and e.state == "idle")

Busy = Callable[[FakeStore, asyncio.Event], Awaitable[tuple[WorkspaceHost, Stop]]]
"""Makes a session saved in a store busy with one activity until a gate opens; returns it and
the event at which that activity is the one in progress."""


class Watch:
    """The events reaching ``session``'s frontend, those ``seen`` so far, and the open
    session's state as last published among them."""

    def __init__(self, session: WorkspaceHost) -> None:
        self._stream = frontend(session).events()
        self.seen: list[Event] = []
        self.state: SessionState | None = None

    async def until(self, done: Callable[[Event], bool]) -> Event:
        """The first event from here on that's ``done``."""
        while True:
            event = (await asyncio.wait_for(anext(self._stream), 10)).payload
            self.seen.append(event)
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


async def running_tasks(store: FakeStore, gate: asyncio.Event) -> tuple[WorkspaceHost, Stop]:
    session, _, _ = session_for(gated_work(gate), store=store)
    await send(session, AddTask(task=TASK))
    await send(session, RunTasks())
    return session, RUNNING


async def reviewing(store: FakeStore, gate: asyncio.Event) -> tuple[WorkspaceHost, Stop]:
    reviewer = gated_work(gate)
    session, _, _ = session_for(
        claims_done, reviewers=(reviewer,), tree=FakeTree(), review=True, store=store
    )
    await send(session, AddTask(task=TASK))
    await send(session, RunTasks())
    started = Stop("reviewer", lambda e: isinstance(e, AgentStarted) and e.role == "reviewer")
    return session, all_of(started, idle("work"))


async def planning(store: FakeStore, gate: asyncio.Event) -> tuple[WorkspaceHost, Stop]:
    session, _, launcher = session_for(store=store)
    launcher.planner_turns = [gated_planner(gate)]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    return session, RUNNING


def saved_earlier() -> FakeStore:
    """A store holding the saved session ``earlier``."""
    store = FakeStore()
    when = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    store.saved.append(SessionRecord(id="earlier", name="Earlier", created=when, last_active=when))
    return store


async def activating(session: WorkspaceHost, watch: Watch, command_id: str) -> Event:
    """The answer to activating the saved session ``earlier`` as ``command_id``."""
    command = ActivateSession(session="earlier")
    await frontend(session).send(CommandEnvelope(command_id=command_id, payload=command))
    return await watch.until(
        lambda e: isinstance(e, CommandAccepted | CommandRefused) and e.command_id == command_id
    )


@pytest.mark.parametrize("busy", [running_tasks, reviewing, planning])
async def test_activating_is_refused_as_busy_exactly_while_the_state_is_running(
    busy: Busy,
) -> None:
    store, gate = saved_earlier(), asyncio.Event()
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


async def test_activating_while_only_the_startup_check_runs_is_accepted_and_idle() -> None:
    store, gate = saved_earlier(), asyncio.Event()
    steps = Steps(True, True)
    steps.gate = gate
    session, _, _ = session_for(verification=checked(steps), check_at_startup=True, store=store)
    await greet(session)
    watch = Watch(session)
    await watch.until(lambda e: isinstance(e, AgentStarted) and e.agent_id == "startup-check")
    accepted = await activating(session, watch, "while-checking")
    gate.set()
    await session.close()

    assert isinstance(accepted, CommandAccepted)
    assert [
        (e.active, e.state)
        for e in watch.seen
        if isinstance(e, SessionStateChanged) and e.session == "earlier"
    ] == [(True, "idle")]
