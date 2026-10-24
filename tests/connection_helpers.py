"""Helpers for driving connections to a backend: sending commands, joining a session and reading
what comes back."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import suppress

from orbit.agent import WorkspaceHost
from orbit.agent.application.connection import SessionConnection
from orbit.protocol import (
    Command,
    CommandEnvelope,
    Event,
    EventEnvelope,
    Session,
    Snapshot,
)
from tests.agent_fakes import HELLO, Backend, attach_to, connect, frontend, host_of, of_type
from tests.loop_quiet import quiet_within

QUIET = 0.05
"""How long a stream stays silent before it counts as drained, in seconds, unless the event loop
is quiet sooner."""


async def stop[T](task: "asyncio.Future[T]") -> None:
    """Cancel ``task`` and wait for it to end."""
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def say(
    connection: Session, command_id: str, command: Command, session: str | None = None
) -> None:
    """Send ``command`` on ``connection`` as ``command_id``, for ``session`` if given."""
    envelope = CommandEnvelope(command_id=command_id, payload=command, session=session)
    await connection.send(envelope)


async def joined(
    session: Backend,
    name: str | None = None,
    *,
    host: WorkspaceHost | None = None,
    read: bool = False,
) -> SessionConnection:
    """A new connection to ``session`` that has said Hello as ``name`` and attached as
    ``<name>-attach`` to the open session of ``host``, or of ``session``'s own workspace if
    that's none, named for that workspace if ``name`` is none, and, if ``read``, read what that
    brought."""
    target = host_of(session if host is None else host)
    name = name or target.workspace_id
    connection = connect(session)
    await say(connection, name, HELLO)
    await say(connection, f"{name}-attach", attach_to(target))
    if read:
        await received(connection)
    return connection


async def quiet[T](stream: AsyncIterator[T]) -> list[T]:
    """What ``stream`` yields until nothing more comes for a moment: until the event loop is
    quiet, or nothing has come for :data:`QUIET` seconds."""
    items: list[T] = []
    while True:
        getting = asyncio.ensure_future(anext(stream))
        waiting = asyncio.ensure_future(quiet_within(QUIET))
        await asyncio.wait({getting, waiting}, return_when=asyncio.FIRST_COMPLETED)
        if getting.done():
            await stop(waiting)
            items.append(getting.result())
            continue
        await stop(getting)
        return items


async def envelopes(session: Session | Backend) -> list[EventEnvelope]:
    """The envelopes that reach ``session`` until nothing more comes for a moment."""
    return await quiet(frontend(session).events())


async def received(session: Session | Backend) -> list[Event]:
    """The events that reach ``session`` until nothing more comes for a moment."""
    return [envelope.payload for envelope in await envelopes(session)]


async def until(session: Session | Backend, done: Callable[[Event], bool]) -> list[Event]:
    """The events reaching ``session`` up to and including the first that's ``done``, each
    within five seconds."""
    stream = frontend(session).events()
    events = [(await asyncio.wait_for(anext(stream), 5)).payload]
    while not done(events[-1]):
        events.append((await asyncio.wait_for(anext(stream), 5)).payload)
    return events


async def published(session: Session | Backend, count: int) -> list[Event]:
    """The next ``count`` events that reach ``session``, each within a second."""
    stream = frontend(session).events()
    return [(await asyncio.wait_for(anext(stream), 1)).payload for _ in range(count)]


def emptied[T](queue: "asyncio.Queue[T]") -> list[T]:
    """Take what ``queue`` holds, without waiting for more."""
    return [queue.get_nowait() for _ in range(queue.qsize())]


def taken(connection: SessionConnection) -> list[EventEnvelope]:
    """Take the envelopes queued for ``connection`` so far."""
    return emptied(connection._queue)


async def views_on_joining(session: Backend) -> list[Event]:
    """The events a new connection to ``session`` gets for its Hello and its Attach to the open
    session, up to its Snapshot."""
    return await until(await joined(session, "join"), of_type(Snapshot))
