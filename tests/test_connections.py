"""Several frontends sharing one session, each through a connection of its own."""

import asyncio

import pytest

from orbit import __version__
from orbit.agent import AgentSession
from orbit.agent.application.connection import (
    QUEUE_LIMIT,
    TOO_FAR_BEHIND,
    ConnectionDropped,
    SessionConnection,
)
from orbit.agent.application.handshake import HELLO_FIRST_MESSAGE
from orbit.protocol import (
    INVALID,
    AddTask,
    Catalog,
    Command,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    Event,
    EventEnvelope,
    History,
    HistoryPage,
    Notice,
    Settings,
    Snapshot,
    TasksChanged,
    TaskSpec,
    Welcome,
)
from tests.agent_fakes import (
    HELLO,
    FakeTree,
    Steps,
    agent_exited,
    events_until,
    publish,
    session_for,
)
from tests.test_startup_check import checked

TASK = TaskSpec(prompt="Add a flag.", settings=Settings(model="opus"))


async def say(connection: SessionConnection, command_id: str, command: Command) -> None:
    """Send ``command`` on ``connection`` as ``command_id``."""
    await connection.send(CommandEnvelope(command_id=command_id, payload=command))


async def welcomed(session: AgentSession, name: str) -> SessionConnection:
    """A new connection to ``session`` that has said Hello as ``name``."""
    connection = session.connect()
    await say(connection, name, HELLO)
    return connection


async def everything(connection: SessionConnection) -> list[Event]:
    """What reached ``connection``, once its stream has ended."""
    return [envelope.payload async for envelope in connection.events()]


async def taken(connection: SessionConnection) -> list[Event]:
    """What reached ``connection`` until nothing more comes for a moment."""
    events: list[Event] = []
    stream = connection.events()
    try:
        while True:
            events.append((await asyncio.wait_for(anext(stream), 0.05)).payload)
    except TimeoutError:
        return events


def welcomes(events: list[Event]) -> list[Welcome]:
    """The ``Welcome``\\s among ``events``."""
    return [e for e in events if isinstance(e, Welcome)]


def kinds(events: list[Event]) -> list[type[Event]]:
    """The types of the catalogs and snapshots among ``events``, in order."""
    return [type(e) for e in events if isinstance(e, Catalog | Snapshot)]


def welcome(command_id: str) -> Welcome:
    return Welcome(command_id=command_id, server_version=__version__)


async def test_each_connection_gets_its_own_welcome_catalog_and_snapshot() -> None:
    session = AgentSession()
    first = await welcomed(session, "first")
    second = await welcomed(session, "second")
    await session.close()

    one, two = await everything(first), await everything(second)
    assert (welcomes(one), welcomes(two)) == ([welcome("first")], [welcome("second")])
    assert (kinds(one), kinds(two)) == ([Catalog, Snapshot], [Catalog, Snapshot])


async def test_a_command_on_one_connection_reaches_every_welcomed_one_with_its_outcome() -> None:
    session = AgentSession()
    first = await welcomed(session, "first")
    second = await welcomed(session, "second")
    await taken(first), await taken(second)

    await say(first, "add", AddTask(task=TASK))

    for events in (await taken(first), await taken(second)):
        assert [type(e) for e in events] == [TasksChanged, CommandAccepted]
        assert events[-1] == CommandAccepted(command_id="add")
    await session.close()


async def test_a_history_page_reaches_only_the_connection_that_asked() -> None:
    session = AgentSession()
    first = await welcomed(session, "first")
    second = await welcomed(session, "second")
    await taken(first), await taken(second)

    await say(first, "page", History(session=session.saved_session.id, before_seq=100))

    assert [type(e) for e in await taken(first)] == [HistoryPage, CommandAccepted]
    assert await taken(second) == [CommandAccepted(command_id="page")]
    await session.close()


async def test_a_command_before_hello_is_refused_on_that_connection_alone() -> None:
    session = AgentSession()
    first = await welcomed(session, "first")
    await taken(first)
    second = session.connect()

    await say(second, "early", AddTask(task=TASK))

    refusal = CommandRefused(command_id="early", code=INVALID, message=HELLO_FIRST_MESSAGE)
    assert await taken(second) == [refusal]
    assert await taken(first) == []
    await session.close()


async def test_a_connection_that_falls_too_far_behind_is_closed_and_the_others_carry_on() -> None:
    session = AgentSession()
    stalled = await welcomed(session, "stalled")
    reading = await welcomed(session, "reading")
    await taken(reading)
    read: list[Event] = []

    async def keep_reading() -> None:
        async for envelope in reading.events():
            read.append(envelope.payload)

    reader = asyncio.create_task(keep_reading())
    for n in range(QUEUE_LIMIT + 1):
        await publish(session, Notice(message=str(n)))
        await asyncio.sleep(0)

    with pytest.raises(ConnectionDropped, match=TOO_FAR_BEHIND):
        await everything(stalled)
    await publish(session, Notice(message="after"))
    await session.close()
    await reader
    assert read == [Notice(message=str(n)) for n in range(QUEUE_LIMIT + 1)] + [
        Notice(message="after")
    ]


async def test_an_unreadable_command_is_refused_to_every_welcomed_connection() -> None:
    session = AgentSession()
    first = await welcomed(session, "first")
    second = await welcomed(session, "second")
    await taken(first), await taken(second)

    await first.refuse("c1", INVALID, "Not a command")

    refusal = CommandRefused(command_id="c1", code=INVALID, message="Not a command")
    assert (await taken(first), await taken(second)) == ([refusal], [refusal])
    await session.close()


async def test_an_unreadable_command_before_hello_is_refused_on_that_connection_alone() -> None:
    session = AgentSession()
    first = await welcomed(session, "first")
    await taken(first)
    second = session.connect()

    await second.refuse("c1", INVALID, "Not a command")

    refusal = CommandRefused(command_id="c1", code=INVALID, message="Not a command")
    assert (await taken(second), await taken(first)) == ([refusal], [])
    await session.close()


async def test_an_unreadable_command_on_a_closed_connection_is_not_refused() -> None:
    session = AgentSession()
    connection = await welcomed(session, "closed")
    await taken(connection)
    await connection.close()

    await connection.refuse("c1", INVALID, "Not a command")

    assert await everything(connection) == []
    await session.close()


async def test_closing_one_connection_leaves_the_others_working() -> None:
    session = AgentSession()
    first = await welcomed(session, "first")
    second = await welcomed(session, "second")
    await taken(second)

    await first.close()
    await say(first, "ignored", AddTask(task=TASK))
    await say(second, "add", AddTask(task=TASK))

    assert await everything(first) == []
    assert [type(e) for e in await taken(second)] == [TasksChanged, CommandAccepted]
    await session.close()


async def test_closing_the_session_ends_every_connections_stream() -> None:
    session = AgentSession()
    first = await welcomed(session, "first")
    second = await welcomed(session, "second")

    await session.close()

    assert welcomes(await everything(first)) == [welcome("first")]
    assert welcomes(await everything(second)) == [welcome("second")]


async def test_the_startup_check_runs_once_for_the_first_connection_only() -> None:
    steps = Steps(True, True)
    tree = FakeTree()
    session, _, _ = session_for(verification=checked(steps), tree=tree, check_at_startup=True)
    await events_until(session, agent_exited("startup-check"))

    later = await welcomed(session, "later")
    await taken(later)

    assert steps.runs == ["Lint", "Unit tests"]
    assert tree.snapshots == ["HEAD"]
    await session.close()


async def test_an_event_for_a_closed_connection_is_dropped() -> None:
    session = AgentSession()
    connection = session.connect()
    await connection.close()

    connection.receive(EventEnvelope(seq=1, payload=Notice(message="late")))

    assert await everything(connection) == []
    await session.close()
