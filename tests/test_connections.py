"""Several frontends sharing one session, each through a connection of its own."""

import asyncio
from collections.abc import Sequence
from dataclasses import replace

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
from orbit.agent.domain.usage import UsageReport
from orbit.protocol import (
    INTERNAL,
    INVALID,
    AddTask,
    Catalog,
    Command,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    ErrorOccurred,
    Event,
    EventEnvelope,
    Hello,
    History,
    HistoryPage,
    NewSession,
    Notice,
    RenameSession,
    SessionStateChanged,
    Settings,
    Snapshot,
    TasksChanged,
    TaskSpec,
    UsageChanged,
    Welcome,
)
from tests.agent_fakes import (
    HELLO,
    FakeStore,
    FakeTree,
    Steps,
    Ticks,
    agent_exited,
    events_until,
    publish,
    session_for,
)
from tests.test_replay import OPEN, notice, store_with
from tests.test_startup_check import checked
from tests.test_usage_monitor import LIMITS, Source

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
    return [envelope.payload for envelope in await taken_envelopes(connection)]


async def taken_envelopes(connection: SessionConnection) -> list[EventEnvelope]:
    """The envelopes that reached ``connection`` until nothing more comes for a moment."""
    envelopes: list[EventEnvelope] = []
    stream = connection.events()
    try:
        while True:
            envelopes.append(await asyncio.wait_for(anext(stream), 0.05))
    except TimeoutError:
        return envelopes


def welcomes(events: list[Event]) -> list[Welcome]:
    """The ``Welcome``\\s among ``events``."""
    return [e for e in events if isinstance(e, Welcome)]


def kinds(events: list[Event]) -> list[type[Event]]:
    """The types of the catalogs and snapshots among ``events``, in order."""
    return [type(e) for e in events if isinstance(e, Catalog | Snapshot)]


def notices(events: list[Event]) -> list[Notice]:
    """The notices among ``events``."""
    return [e for e in events if isinstance(e, Notice)]


def viewer_counts(events: list[Event]) -> list[int]:
    """The viewers each session state change among ``events`` counts."""
    return [e.viewers for e in events if isinstance(e, SessionStateChanged)]


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


async def test_a_new_session_gives_every_connection_its_catalog_and_snapshot() -> None:
    session = AgentSession()
    first = await welcomed(session, "first")
    second = await welcomed(session, "second")
    await taken(first), await taken(second)

    await say(first, "new", NewSession(name="Next"))

    for events in (await taken(first), await taken(second)):
        switch = [e for e in events if isinstance(e, Catalog | Snapshot | CommandAccepted)]
        assert [type(e) for e in switch] == [Catalog, Snapshot, CommandAccepted]
        snapshot = switch[1]
        assert isinstance(snapshot, Snapshot)
        assert snapshot.session.id == session.saved_session.id
    await session.close()


def catalog_names(events: list[Event]) -> list[list[str]]:
    """The names of the sessions each catalog among ``events`` holds."""
    catalogs = [e for e in events if isinstance(e, Catalog)]
    return [[s.name for w in c.workspaces for s in w.sessions] for c in catalogs]


async def test_a_rename_gives_every_connection_the_updated_catalog() -> None:
    session = AgentSession()
    first = await welcomed(session, "first")
    second = await welcomed(session, "second")
    await taken(first), await taken(second)

    await say(first, "rename", RenameSession(session_id=session.saved_session.id, name="Kept"))

    for events in (await taken(first), await taken(second)):
        assert catalog_names(events) == [["Kept"]]
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
    assert notices(read) == [Notice(message=str(n)) for n in range(QUEUE_LIMIT + 1)] + [
        Notice(message="after")
    ]
    assert viewer_counts(read) == [1]


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
    left, *answered = await taken(second)
    assert isinstance(left, SessionStateChanged) and left.viewers == 1
    assert [type(e) for e in answered] == [TasksChanged, CommandAccepted]
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


def greeting(events: list[Event]) -> list[Event]:
    """The notices, usage and errors among ``events``, which only a greeting sends here."""
    return [e for e in events if isinstance(e, Notice | UsageChanged | ErrorOccurred)]


async def test_a_second_hello_greets_only_the_connection_saying_it() -> None:
    usage = Source(UsageReport(LIMITS))
    session = AgentSession(workspace="w", startup_notes=["n"], usage=usage)
    first = await welcomed(session, "first")
    seen = await taken(first)
    while UsageChanged(limits=LIMITS) not in seen:
        seen += await taken(first)

    second = await welcomed(session, "second")

    assert greeting(await taken(first)) == []
    assert greeting(await taken(second)) == [
        UsageChanged(limits=LIMITS),
        Notice(message="Workspace: w"),
        Notice(message="n"),
    ]
    await session.close()


async def test_a_second_hellos_failed_greeting_step_reaches_only_that_connection() -> None:
    session = AgentSession()
    first = await welcomed(session, "first")
    await taken(first)

    async def no_leftovers() -> Sequence[str]:
        raise RuntimeError("no leftovers")

    session._greeting = replace(session._greeting, leftovers=no_leftovers)
    second = await welcomed(session, "second")

    message = "Orbit failed to finish greeting the frontend: no leftovers"
    assert greeting(await taken(second)) == [ErrorOccurred(code=INTERNAL, message=message)]
    assert greeting(await taken(first)) == []
    await session.close()


async def test_a_greetings_notices_are_not_logged() -> None:
    session = AgentSession(store=FakeStore(), workspace="w", startup_notes=["n"])
    first = await welcomed(session, "first")
    second = await welcomed(session, "second")
    await taken(first), await taken(second)
    await publish(session, Notice(message="logged"))
    await taken(first)

    transcript = await session._events.transcript(session.saved_session.id)
    await session.close()

    assert notices([e.payload for e in transcript]) == [Notice(message="logged")]


LIVE = Notice(message="live")


async def rejoined(store: FakeStore, since: int, live: Sequence[Event]) -> SessionConnection:
    """A connection to a session over ``store`` that has said Hello with ``since``, the ``live``
    events published while what it missed was being read."""
    session = AgentSession(store=store, clock=Ticks(), new_id=lambda: OPEN)
    read = session._greeting.missed

    async def slow(seen: int) -> Sequence[EventEnvelope] | None:
        missed = await read(seen)
        for event in live:
            await publish(session, event)
        return missed

    session._greeting = replace(session._greeting, missed=slow)
    connection = session.connect()
    hello = Hello(client="orbit-tests", protocol_version=HELLO.protocol_version, since=since)
    await say(connection, "rejoin", hello)
    return connection


def after_catalog(envelopes: list[EventEnvelope]) -> list[EventEnvelope]:
    """The envelopes after the first ``Catalog`` among ``envelopes``."""
    payloads = [envelope.payload for envelope in envelopes]
    return envelopes[[type(p) for p in payloads].index(Catalog) + 1 :]


async def test_events_published_while_catching_up_follow_the_ones_resent() -> None:
    store = store_with(*(notice(seq) for seq in range(10, 21)))
    connection = await rejoined(store, 15, [LIVE])

    envelopes = await taken_envelopes(connection)

    assert isinstance(envelopes[0].payload, Welcome)
    rest = after_catalog(envelopes)
    assert [e.payload for e in rest[:6]] == [n.payload for n in map(notice, range(16, 21))] + [LIVE]
    seqs = [e.seq for e in rest]
    assert seqs == sorted(set(seqs))


async def test_events_published_while_finding_a_snapshot_is_needed_follow_welcome() -> None:
    store = store_with(*(notice(seq, "other") for seq in range(1, 16)))
    connection = await rejoined(store, 15, [LIVE])

    events = await taken(connection)

    assert isinstance(events[0], Welcome)
    assert LIVE in events and Snapshot in [type(e) for e in events]


async def test_a_connection_whose_held_events_overflow_is_closed_as_too_far_behind() -> None:
    store = store_with(*(notice(seq) for seq in range(10, 21)))
    live = [Notice(message=str(n)) for n in range(QUEUE_LIMIT + 1)]
    connection = await rejoined(store, 15, live)

    with pytest.raises(ConnectionDropped, match=TOO_FAR_BEHIND):
        await everything(connection)
