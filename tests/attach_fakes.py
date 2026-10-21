"""Fakes for connections that attach to a workspace host's sessions."""

import asyncio
from datetime import UTC, datetime

from orbit.agent import WorkspaceHost
from orbit.agent.application.connection import SessionConnection
from orbit.agent.domain.sessions import SessionRecord
from orbit.protocol import (
    AssistantMessage,
    Attach,
    Command,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    Event,
    EventEnvelope,
    SessionStateChanged,
    Snapshot,
    TaskSpec,
    TaskState,
)
from tests.agent_fakes import HELLO, WORKSPACE, FakeStore, Ticks, attach_to, connect

OPEN = "today-id"
"""The id of the workspace's open session."""


EARLIER = "earlier"
"""The id of the saved session that isn't open."""


WHEN = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


TASK = TaskSpec(prompt="Add a flag.")


SAVED_TASK = TaskState(id="t1", task=TASK, status="completed")


LOGGED = tuple(
    EventEnvelope(seq=seq, session=EARLIER, payload=AssistantMessage(turn_id="turn", text=text))
    for seq, text in ((1, "First."), (2, "Second."))
)


def stored() -> FakeStore:
    """A store holding the session ``earlier``, named "old work", with a task and two logged
    events."""
    store = FakeStore()
    record = SessionRecord(
        id=EARLIER, name="old work", created=WHEN, last_active=WHEN, tasks=(SAVED_TASK,)
    )
    store.saved.append(record)
    store.logged += LOGGED
    return store


def workspace(store: FakeStore) -> WorkspaceHost:
    """A session saved in ``store``, its open one named "today"."""
    return WorkspaceHost(store=store, clock=Ticks(), new_id=lambda: OPEN, name="today")


async def say(
    connection: SessionConnection, command_id: str, command: Command, session: str | None = None
) -> None:
    envelope = CommandEnvelope(command_id=command_id, payload=command, session=session)
    await connection.send(envelope)


async def envelopes(connection: SessionConnection) -> list[EventEnvelope]:
    """What reached ``connection`` until nothing more comes for a moment."""
    taken: list[EventEnvelope] = []
    stream = connection.events()
    try:
        while True:
            taken.append(await asyncio.wait_for(anext(stream), 0.05))
    except TimeoutError:
        return taken


async def taken(connection: SessionConnection) -> list[Event]:
    """The events that reached ``connection`` until nothing more comes for a moment."""
    return [envelope.payload for envelope in await envelopes(connection)]


async def joined(session: WorkspaceHost, name: str) -> SessionConnection:
    """A new connection to ``session`` that has said Hello as ``name`` and attached to the open
    session, and read what that brought."""
    connection = connect(session)
    await say(connection, name, HELLO)
    await say(connection, f"{name}-attach", attach_to(session))
    await taken(connection)
    return connection


async def viewing(session: WorkspaceHost, name: str) -> SessionConnection:
    """A connection to ``session`` that has attached to ``earlier``, and read what that
    brought."""
    connection = await joined(session, name)
    await say(connection, "attach", Attach(workspace=WORKSPACE, session=EARLIER))
    await taken(connection)
    return connection


def snapshot_in(events: list[Event]) -> Snapshot:
    [snapshot] = [e for e in events if isinstance(e, Snapshot)]
    return snapshot


def outcome(events: list[Event]) -> CommandAccepted | CommandRefused:
    [last] = [e for e in events if isinstance(e, CommandAccepted | CommandRefused)]
    return last


def states(events: list[Event]) -> list[tuple[str, bool, int]]:
    """Each session's state change among ``events``, as its session, whether it's active and
    its viewers."""
    changes = [e for e in events if isinstance(e, SessionStateChanged)]
    return [(e.session, e.active, e.viewers) for e in changes]
