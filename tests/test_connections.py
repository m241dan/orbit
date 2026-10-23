"""Several frontends sharing one session, each through a connection of its own."""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import replace

import pytest

from orbit import __version__
from orbit.agent import WorkspaceHost
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
    Attach,
    Catalog,
    CommandAccepted,
    CommandRefused,
    DeleteSession,
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
    WorkspaceEntry,
)
from tests.agent_fakes import (
    HELLO,
    LIMITS,
    WORKSPACE,
    FakeStore,
    FakeTree,
    Source,
    Steps,
    Ticks,
    agent_exited,
    attach_to,
    checked,
    close,
    connect,
    control_plane,
    events_until,
    install_greeting,
    notice,
    publish,
    served,
    session_for,
)
from tests.attach_fakes import EARLIER, stored, workspace
from tests.connection_helpers import envelopes, joined, received, say
from tests.session_builders import OPEN, store_logging

TASK = TaskSpec(prompt="Add a flag.", settings=Settings(model="opus"))


async def everything(connection: SessionConnection) -> list[Event]:
    """What reached ``connection``, once its stream has ended."""
    return [envelope.payload async for envelope in connection.events()]


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
    session = WorkspaceHost()
    first = await joined(session, "first")
    second = await joined(session, "second")
    await session.close()

    one, two = await everything(first), await everything(second)
    assert (welcomes(one), welcomes(two)) == ([welcome("first")], [welcome("second")])
    assert (kinds(one), kinds(two)) == ([Catalog, Snapshot], [Catalog, Snapshot])


async def test_a_command_on_one_connection_reaches_every_welcomed_one_with_its_outcome() -> None:
    session = WorkspaceHost()
    first = await joined(session, "first")
    second = await joined(session, "second")
    await received(first), await received(second)

    await say(first, "add", AddTask(task=TASK))

    for events in (await received(first), await received(second)):
        assert [type(e) for e in events] == [TasksChanged, CommandAccepted]
        assert events[-1] == CommandAccepted(command_id="add")
    await session.close()


async def test_a_history_page_reaches_only_the_connection_that_asked() -> None:
    session = WorkspaceHost()
    first = await joined(session, "first")
    second = await joined(session, "second")
    await received(first), await received(second)

    await say(first, "page", History(session=session.saved_session.id, before_seq=100))

    assert [type(e) for e in await received(first)] == [HistoryPage, CommandAccepted]
    assert await received(second) == [CommandAccepted(command_id="page")]
    await session.close()


async def test_a_new_session_gives_every_connection_its_catalog_and_snapshot() -> None:
    session = WorkspaceHost()
    first = await joined(session, "first")
    second = await joined(session, "second")
    await received(first), await received(second)

    await say(first, "new", NewSession(name="Next"))

    for events in (await received(first), await received(second)):
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
    session = WorkspaceHost()
    first = await joined(session, "first")
    second = await joined(session, "second")
    await received(first), await received(second)

    await say(first, "rename", RenameSession(session_id=session.saved_session.id, name="Kept"))

    for events in (await received(first), await received(second)):
        assert catalog_names(events) == [["Kept"]]
    await session.close()


async def test_a_command_before_hello_is_refused_on_that_connection_alone() -> None:
    session = WorkspaceHost()
    first = await joined(session, "first")
    await received(first)
    second = connect(session)

    await say(second, "early", AddTask(task=TASK))

    refusal = CommandRefused(command_id="early", code=INVALID, message=HELLO_FIRST_MESSAGE)
    assert await received(second) == [refusal]
    assert await received(first) == []
    await session.close()


async def test_a_connection_that_falls_too_far_behind_is_closed_and_the_others_carry_on() -> None:
    session = WorkspaceHost()
    stalled = await joined(session, "stalled")
    reading = await joined(session, "reading")
    await received(reading)
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
    session = WorkspaceHost()
    first = await joined(session, "first")
    second = await joined(session, "second")
    await received(first), await received(second)

    await first.refuse("c1", INVALID, "Not a command")

    refusal = CommandRefused(command_id="c1", code=INVALID, message="Not a command")
    assert (await received(first), await received(second)) == ([refusal], [refusal])
    await session.close()


async def test_an_unreadable_command_before_hello_is_refused_on_that_connection_alone() -> None:
    session = WorkspaceHost()
    first = await joined(session, "first")
    await received(first)
    second = connect(session)

    await second.refuse("c1", INVALID, "Not a command")

    refusal = CommandRefused(command_id="c1", code=INVALID, message="Not a command")
    assert (await received(second), await received(first)) == ([refusal], [])
    await session.close()


async def test_an_unreadable_command_on_a_closed_connection_is_not_refused() -> None:
    session = WorkspaceHost()
    connection = await joined(session, "closed")
    await received(connection)
    await connection.close()

    await connection.refuse("c1", INVALID, "Not a command")

    assert await everything(connection) == []
    await session.close()


async def test_closing_one_connection_leaves_the_others_working() -> None:
    session = WorkspaceHost()
    first = await joined(session, "first")
    second = await joined(session, "second")
    await received(second)

    await first.close()
    await say(first, "ignored", AddTask(task=TASK))
    await say(second, "add", AddTask(task=TASK))

    assert await everything(first) == []
    left, *answered = await received(second)
    assert isinstance(left, SessionStateChanged) and left.viewers == 1
    assert [type(e) for e in answered] == [TasksChanged, CommandAccepted]
    await session.close()


async def test_closing_the_session_ends_every_connections_stream() -> None:
    session = WorkspaceHost()
    first = await joined(session, "first")
    second = await joined(session, "second")

    await session.close()

    assert welcomes(await everything(first)) == [welcome("first")]
    assert welcomes(await everything(second)) == [welcome("second")]


async def test_a_connection_made_once_the_control_plane_closed_ends_and_ignores_commands() -> None:
    session = WorkspaceHost()
    await control_plane(session).close()
    connection = connect(session)

    await say(connection, "late", HELLO)

    assert await everything(connection) == []


async def test_the_startup_check_runs_once_for_the_first_connection_only() -> None:
    steps = Steps(True, True)
    tree = FakeTree()
    session, _, _ = session_for(verification=checked(steps), tree=tree, check_at_startup=True)
    await events_until(session, agent_exited("startup-check"))

    later = await joined(session, "later")
    await received(later)

    assert steps.runs == ["Lint", "Unit tests"]
    assert tree.snapshots == ["HEAD"]
    await session.close()


async def test_an_event_for_a_closed_connection_is_dropped() -> None:
    session = WorkspaceHost()
    connection = connect(session)
    await connection.close()

    connection.receive(EventEnvelope(seq=1, payload=Notice(message="late")))

    assert await everything(connection) == []
    await session.close()


def greeting(events: list[Event]) -> list[Event]:
    """The notices, usage and errors among ``events``, which only a greeting sends here."""
    return [e for e in events if isinstance(e, Notice | UsageChanged | ErrorOccurred)]


async def test_a_second_hello_greets_only_the_connection_saying_it() -> None:
    usage = Source(UsageReport(LIMITS))
    session = WorkspaceHost(workspace="w", startup_notes=["n"])
    served(session, usage)
    first = await joined(session, "first")
    seen = await received(first)
    while UsageChanged(limits=LIMITS) not in seen:
        seen += await received(first)

    second = await joined(session, "second")

    assert greeting(await received(first)) == []
    assert greeting(await received(second)) == [
        UsageChanged(limits=LIMITS),
        Notice(message="Workspace: w"),
        Notice(message="n"),
    ]
    await close(session)


async def test_a_second_hellos_failed_greeting_step_reaches_only_that_connection() -> None:
    session = WorkspaceHost()
    first = await joined(session, "first")
    await received(first)

    async def no_leftovers() -> Sequence[str]:
        raise RuntimeError("no leftovers")

    install_greeting(session, replace(session.greeting, leftovers=no_leftovers))
    second = await joined(session, "second")

    message = "Orbit failed to finish greeting the frontend: no leftovers"
    assert greeting(await received(second)) == [ErrorOccurred(code=INTERNAL, message=message)]
    assert greeting(await received(first)) == []
    await session.close()


async def test_a_greetings_notices_are_not_logged() -> None:
    session = WorkspaceHost(store=FakeStore(), workspace="w", startup_notes=["n"])
    first = await joined(session, "first")
    second = await joined(session, "second")
    await received(first), await received(second)
    await publish(session, Notice(message="logged"))
    await received(first)

    transcript = await session.outbox.transcript(session.saved_session.id)
    await session.close()

    assert notices([e.payload for e in transcript.events]) == [Notice(message="logged")]


LIVE = Notice(message="live")


def unchecked(connection: SessionConnection) -> None:
    """Check nothing of ``connection``."""


async def rejoined(
    store: FakeStore,
    since: int,
    live: Sequence[Event],
    check: Callable[[SessionConnection], None] = unchecked,
) -> SessionConnection:
    """A connection to a session over ``store`` that has said Hello with ``since`` and attached
    to the open session, the ``live`` events published while what it missed was being read,
    after which it had ``check`` see the connection, before what it missed was returned."""
    session = WorkspaceHost(store=store, clock=Ticks(), new_id=lambda: OPEN)
    read = session.greeting.missed

    async def slow(seen: int) -> Sequence[EventEnvelope] | None:
        missed = await read(seen)
        for event in live:
            await publish(session, event)
        check(connection)
        return missed

    install_greeting(session, replace(session.greeting, missed=slow))
    connection = connect(session)
    hello = Hello(client="orbit-tests", protocol_version=HELLO.protocol_version, since=since)
    await say(connection, "rejoin", hello)
    await say(connection, "rejoin-attach", attach_to(session))
    return connection


def after_catalog(envelopes: list[EventEnvelope]) -> list[EventEnvelope]:
    """The envelopes after the first ``Catalog`` among ``envelopes``."""
    payloads = [envelope.payload for envelope in envelopes]
    return envelopes[[type(p) for p in payloads].index(Catalog) + 1 :]


async def test_events_published_while_catching_up_follow_the_ones_resent() -> None:
    store = store_logging(*(notice(seq, OPEN) for seq in range(10, 21)))
    connection = await rejoined(store, 15, [LIVE])

    sent = await envelopes(connection)

    assert isinstance(sent[0].payload, Welcome)
    rest = after_catalog(sent)
    assert [e.payload for e in rest[:6]] == [n.payload for n in map(notice, range(16, 21))] + [LIVE]
    seqs = [e.seq for e in rest]
    assert seqs == sorted(set(seqs))


async def test_events_published_while_finding_a_snapshot_is_needed_follow_welcome() -> None:
    store = store_logging(*(notice(seq, "other") for seq in range(1, 16)))
    connection = await rejoined(store, 15, [LIVE])

    events = await received(connection)

    assert isinstance(events[0], Welcome)
    assert LIVE in events and Snapshot in [type(e) for e in events]


async def test_a_connection_whose_held_events_overflow_is_closed_as_too_far_behind() -> None:
    store = store_logging(*(notice(seq, OPEN) for seq in range(10, 21)))
    live = [Notice(message=str(n)) for n in range(QUEUE_LIMIT + 1)]
    while_held: list[tuple[bool, int]] = []

    def check(connection: SessionConnection) -> None:
        while_held.append((connection._dropped is not None, connection._handshake.held))

    connection = await rejoined(store, 15, live, check)

    assert while_held == [(True, QUEUE_LIMIT - 2)]  # The Welcome and the Catalog, unread.
    with pytest.raises(ConnectionDropped, match=TOO_FAR_BEHIND):
        await everything(connection)


async def test_a_catalog_from_a_task_a_command_started_reaches_every_connection() -> None:
    session = WorkspaceHost()
    first = await joined(session, "first")
    catalog = next(e for e in await received(first) if isinstance(e, Catalog))
    asker = await joined(session, "asker")
    await received(asker)
    go = asyncio.Event()
    started: list[asyncio.Task[None]] = []

    async def later() -> None:
        await go.wait()
        await publish(session, catalog)

    async def spawned(*args: object) -> None:
        started.append(asyncio.create_task(later()))

    asker._answer = spawned
    await say(asker, "spawn", AddTask(task=TASK))
    await asker.close()
    go.set()
    await started[0]

    assert [e for e in await received(first) if isinstance(e, Catalog)] == [catalog]
    await session.close()


async def test_a_new_session_gives_the_others_a_catalog_listing_it() -> None:
    session = WorkspaceHost()
    asker = await joined(session, "asker")
    other = await joined(session, "other")
    await received(asker), await received(other)

    await say(asker, "new", NewSession(name="Next"))
    asked, seen = await received(asker), await received(other)
    await session.close()

    assert kinds(asked) == [Catalog, Snapshot]
    assert catalog_names(asked) == catalog_names(seen) == [["Next"]]


async def test_a_delete_gives_every_connection_a_catalog_without_the_session() -> None:
    session = workspace(stored())
    asker = await joined(session, "asker", read=True)
    other = await joined(session, "other", read=True)

    await say(asker, "delete", DeleteSession(session_id=EARLIER))
    asked, seen = await received(asker), await received(other)
    await session.close()

    assert catalog_names(asked) == catalog_names(seen) == [["today"]]


async def test_a_closed_connection_gets_no_catalog_after_a_rename() -> None:
    session = WorkspaceHost()
    asker = await joined(session, "asker")
    closed = await joined(session, "closed")
    await received(asker), await received(closed)
    await closed.close()

    await say(asker, "rename", RenameSession(session_id=session.saved_session.id, name="Kept"))

    assert await everything(closed) == []
    assert catalog_names(await received(asker)) == [["Kept"]]
    await session.close()


async def test_a_connection_not_yet_welcomed_gets_only_its_greetings_catalog() -> None:
    session = WorkspaceHost()
    asker = await joined(session, "asker")
    await received(asker)
    go = asyncio.Event()
    fetch = session.entry

    async def slow() -> WorkspaceEntry:
        await go.wait()
        return await fetch()

    session.entry = slow  # type: ignore[method-assign]
    joining = connect(session)
    greeting = asyncio.create_task(say(joining, "joining", HELLO))
    await asyncio.sleep(0.01)
    await say(asker, "rename", RenameSession(session_id=session.saved_session.id, name="Kept"))
    go.set()
    await greeting

    assert catalog_names(await received(joining)) == [["Kept"]]
    await session.close()


async def test_attaching_to_the_open_session_gives_only_the_asker_a_snapshot() -> None:
    session = workspace(stored())
    asker = await joined(session, "asker", read=True)
    other = await joined(session, "other", read=True)

    await say(asker, "attach", Attach(workspace=WORKSPACE, session=session.saved_session.id))
    asked, seen = await received(asker), await received(other)
    await session.close()

    assert kinds(asked) == [Snapshot]
    assert kinds(seen) == []
