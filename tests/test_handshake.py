"""The handshake every stream opens with: Hello from the frontend, Welcome and the catalog from
the control plane, then the first Attach, answered with the session."""

import asyncio

from orbit import __version__
from orbit.agent import Verification, WorkspaceHost
from orbit.agent.application.handshake import HELLO_AGAIN_MESSAGE, HELLO_FIRST_MESSAGE
from orbit.agent.domain.usage import UsageReport
from orbit.agent.domain.workspaces import ATTACH_FIRST
from orbit.protocol import (
    INCOMPATIBLE,
    INTERNAL,
    INVALID,
    PROTOCOL_VERSION,
    UNKNOWN,
    WRONG_SESSION,
    AddTask,
    Attach,
    Catalog,
    Command,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    ErrorOccurred,
    Event,
    Hello,
    Notice,
    RunTasks,
    SessionStateChanged,
    Snapshot,
    TasksChanged,
    TaskSpec,
    Welcome,
)
from tests.agent_fakes import (
    ATTACH_ID,
    HELLO,
    HELLO_ID,
    LIMITS,
    STEP,
    WORKSPACE,
    FakeStore,
    FakeTree,
    Source,
    Steps,
    attach_to,
    board,
    events_until,
    frontend,
    greet,
    nothing_queued,
    of_type,
    queued,
    send,
    session_for,
)


async def send_as(
    session: WorkspaceHost, command_id: str, command: Command, /, **fields: str
) -> None:
    """Send ``command`` to ``session`` as ``command_id``, with the envelope's other ``fields``,
    greeted or not."""
    await frontend(session).send(CommandEnvelope(command_id=command_id, payload=command, **fields))


async def published(session: WorkspaceHost, count: int) -> list[Event]:
    """The next ``count`` events ``session`` publishes."""
    stream = frontend(session).events()
    return [(await asyncio.wait_for(anext(stream), 1)).payload for _ in range(count)]


async def silent(session: WorkspaceHost) -> bool:
    """Whether ``session`` publishes nothing for a moment."""
    try:
        await asyncio.wait_for(anext(frontend(session).events()), 0.05)
    except TimeoutError:
        return True
    return False


def version(protocol_version: int) -> Hello:
    return Hello(client="orbit-tests", protocol_version=protocol_version)


async def test_nothing_is_published_before_hello() -> None:
    session = WorkspaceHost(workspace="/work")
    assert await silent(session)
    await session.close()


async def test_hello_is_welcomed_with_a_catalog_and_nothing_else() -> None:
    session, _, _ = session_for(
        verification=Verification((STEP,), Steps()), usage=Source(UsageReport(LIMITS))
    )
    await send_as(session, HELLO_ID, HELLO)
    events = await published(session, 2)
    quiet = await silent(session)
    await session.close()

    assert events[0] == Welcome(command_id=HELLO_ID, server_version=__version__)
    assert isinstance(events[1], Catalog)
    assert quiet


async def test_attaching_after_hello_gets_the_snapshot_the_workspace_note_and_a_viewer() -> None:
    session = WorkspaceHost(
        workspace="/work", verification=Verification((STEP,), Steps()), new_id=lambda: "s1"
    )
    await greet(session)
    events = await published(session, 6)
    await session.close()

    assert isinstance(events[2], Snapshot)
    assert [step.name for step in events[2].steps] == [STEP.name]
    assert events[3] == Notice(message="Workspace: /work")
    assert events[4] == CommandAccepted(command_id=ATTACH_ID)
    assert events[5] == SessionStateChanged(session="s1", active=True, state="idle", viewers=1)
    assert nothing_queued(session)


async def test_the_startup_notes_follow_the_workspace_note() -> None:
    notes = ("Imported 2 saved sessions.", "Something else.")
    session = WorkspaceHost(workspace="/work", startup_notes=notes)
    await greet(session)
    events = await published(session, 6)
    await session.close()

    assert events[3:] == [
        Notice(message="Workspace: /work"),
        Notice(message="Imported 2 saved sessions."),
        Notice(message="Something else."),
    ]


async def test_a_command_before_hello_is_refused_as_invalid_and_changes_nothing() -> None:
    session, _, launcher = session_for()
    await send_as(session, "add", AddTask(task=TaskSpec(prompt="Add a flag.")))
    await send_as(session, "run", RunTasks())
    refusals = await published(session, 2)
    await send(session, AddTask(task=TaskSpec(prompt="Add another.")))
    tasks = await events_until(session, of_type(TasksChanged))
    await session.close()

    assert refusals == [
        CommandRefused(command_id="add", code=INVALID, message=HELLO_FIRST_MESSAGE),
        CommandRefused(command_id="run", code=INVALID, message=HELLO_FIRST_MESSAGE),
    ]
    assert [t.task.prompt for t in board(tasks)] == ["Add another."]
    assert launcher.launches == []


async def test_a_command_before_attaching_is_refused_as_invalid_and_runs_nothing() -> None:
    session, _, launcher = session_for()
    await send_as(session, HELLO_ID, HELLO)
    await published(session, 2)
    await send_as(session, "run", RunTasks())
    refused = await published(session, 1)
    quiet = await silent(session)
    await session.close()

    assert refused == [CommandRefused(command_id="run", code=INVALID, message=ATTACH_FIRST)]
    assert quiet
    assert launcher.launches == []


OTHER_WORKSPACE = f"No workspace 'other'; the workspace served is {WORKSPACE}."
OTHER_SESSION = "Session 'other' isn't open; the open session is session-1."


async def test_a_misaddressed_command_is_refused_for_its_address() -> None:
    session = WorkspaceHost(new_id=lambda: "session-1")
    await greet(session)
    await published(session, 5)
    await send_as(session, "c1", AddTask(task=TaskSpec(prompt="Add a flag.")), workspace="other")
    await send_as(session, "c2", AddTask(task=TaskSpec(prompt="Add a flag.")), session="other")
    await send_as(session, "c3", AddTask(task=TaskSpec(prompt="Add a flag.")), workspace=WORKSPACE)
    outcomes = [e for e in await published(session, 4) if not isinstance(e, TasksChanged)]
    await session.close()

    assert outcomes == [
        CommandRefused(command_id="c1", code=UNKNOWN, message=OTHER_WORKSPACE),
        CommandRefused(command_id="c2", code=WRONG_SESSION, message=OTHER_SESSION),
        CommandAccepted(command_id="c3"),
    ]


async def test_an_attach_to_a_workspace_not_served_is_refused_as_unknown() -> None:
    session = WorkspaceHost(new_id=lambda: "session-1")
    await send_as(session, HELLO_ID, HELLO)
    await published(session, 2)
    await send_as(session, "a1", Attach(workspace="other", session="session-1"))
    refused = await published(session, 1)
    quiet = await silent(session)
    await session.close()

    assert refused == [CommandRefused(command_id="a1", code=UNKNOWN, message=OTHER_WORKSPACE)]
    assert quiet


async def test_an_attach_to_a_session_there_is_none_of_is_refused_and_leaves_it_unattached() -> (
    None
):
    session = WorkspaceHost(store=FakeStore(), new_id=lambda: "session-1")
    await send_as(session, HELLO_ID, HELLO)
    await published(session, 2)
    await send_as(session, "a1", Attach(workspace=WORKSPACE, session="gone"))
    await send_as(session, "run", RunTasks())
    events = await published(session, 2)
    await session.close()

    assert isinstance(events[0], CommandRefused) and events[0].code == UNKNOWN
    assert events[1] == CommandRefused(command_id="run", code=INVALID, message=ATTACH_FIRST)


async def test_a_second_hello_is_refused_as_invalid() -> None:
    session = WorkspaceHost()
    await greet(session)
    await send_as(session, "again", HELLO)
    events = await published(session, 6)
    await session.close()

    assert events[-1] == CommandRefused(
        command_id="again", code=INVALID, message=HELLO_AGAIN_MESSAGE
    )


async def test_a_version_1_hello_is_refused_and_ends_the_stream() -> None:
    session = WorkspaceHost()
    await send_as(session, "hello", version(1))
    await send_as(session, "add", AddTask(task=TaskSpec(prompt="Add a flag.")))
    await send_as(session, "again", HELLO)
    events = [envelope.payload async for envelope in frontend(session).events()]
    await session.close()

    message = f"Protocol version 1 isn't supported; this orbit speaks version {PROTOCOL_VERSION}."
    assert events == [CommandRefused(command_id="hello", code=INCOMPATIBLE, message=message)]
    assert nothing_queued(session)


async def test_the_startup_check_and_usage_monitor_wait_for_the_first_attach() -> None:
    steps, source = Steps(True), Source(UsageReport(LIMITS))
    session, _, _ = session_for(
        verification=Verification((STEP,), steps), check_at_startup=True, usage=source
    )
    await send_as(session, HELLO_ID, HELLO)
    await asyncio.sleep(0.05)
    assert (steps.runs, source.calls) == ([], 0)

    await send_as(session, ATTACH_ID, attach_to(session))
    await asyncio.sleep(0.05)
    await session.close()
    assert (steps.runs, source.calls) == ([STEP.name], 1)


async def test_closing_a_session_never_greeted_stops_cleanly() -> None:
    steps, source = Steps(True), Source(UsageReport(LIMITS))
    session, host, _ = session_for(
        verification=Verification((STEP,), steps), check_at_startup=True, usage=source
    )
    await session.close()

    assert (steps.runs, source.calls) == ([], 0)
    assert host.starts == host.stops


async def test_an_attach_whose_leftovers_fail_is_accepted_once_and_reports_an_error() -> None:
    tree = FakeTree()
    tree.failures["leftovers"] = RuntimeError("git stash list failed")
    session, _, _ = session_for(tree=tree)
    await greet(session)
    events = queued(session)
    await send_as(session, "add", AddTask(task=TaskSpec(prompt="Add a flag.")))
    accepted = await events_until(session, of_type(CommandAccepted))
    await session.close()

    message = "Orbit failed to finish greeting the frontend: git stash list failed"
    outcomes = [e for e in events if isinstance(e, Welcome | CommandAccepted | CommandRefused)]
    assert outcomes == [
        Welcome(command_id=HELLO_ID, server_version=__version__),
        CommandAccepted(command_id=ATTACH_ID),
    ]
    assert ErrorOccurred(code=INTERNAL, message=message) in events
    assert accepted[-1] == CommandAccepted(command_id="add")
