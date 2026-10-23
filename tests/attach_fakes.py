"""Fakes for connections that attach to a workspace host's sessions."""

from datetime import UTC, datetime

from orbit.agent import WorkspaceHost
from orbit.agent.application.connection import SessionConnection
from orbit.agent.domain.sessions import SessionRecord
from orbit.protocol import (
    AssistantMessage,
    Attach,
    CommandAccepted,
    CommandRefused,
    Event,
    EventEnvelope,
    SessionStateChanged,
    Snapshot,
    TaskState,
)
from tests.agent_fakes import WORKSPACE, FakeStore, Ticks
from tests.connection_helpers import joined, received, say
from tests.server_fakes import TASK

OPEN = "today-id"
"""The id of the workspace's open session."""


EARLIER = "earlier"
"""The id of the saved session that isn't open."""


WHEN = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


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


async def viewing(session: WorkspaceHost, name: str) -> SessionConnection:
    """A connection to ``session`` that has attached to ``earlier``, and read what that
    brought."""
    connection = await joined(session, name, read=True)
    await say(connection, "attach", Attach(workspace=WORKSPACE, session=EARLIER))
    await received(connection)
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
