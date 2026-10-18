"""The handshake every stream opens with: Hello from the frontend, Welcome from the session."""

import asyncio

from orbit import __version__
from orbit.agent import AgentSession, Verification
from orbit.agent.application.session import HELLO_AGAIN_MESSAGE, HELLO_FIRST_MESSAGE
from orbit.agent.domain.usage import UsageReport
from orbit.protocol import (
    INCOMPATIBLE,
    INTERNAL,
    INVALID,
    PROTOCOL_VERSION,
    UNKNOWN,
    WRONG_SESSION,
    AddTask,
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
    Snapshot,
    TasksChanged,
    TaskSpec,
    Welcome,
)
from tests.agent_fakes import (
    HELLO,
    HELLO_ID,
    STEP,
    FakeTree,
    Steps,
    board,
    events_until,
    greet,
    nothing_queued,
    of_type,
    queued,
    send,
    session_for,
)
from tests.test_usage_monitor import LIMITS, Source


async def send_as(
    session: AgentSession, command_id: str, command: Command, /, **fields: str
) -> None:
    """Send ``command`` to ``session`` as ``command_id``, with the envelope's other ``fields``,
    greeted or not."""
    await session.send(CommandEnvelope(command_id=command_id, payload=command, **fields))


async def published(session: AgentSession, count: int) -> list[Event]:
    """The next ``count`` events ``session`` publishes."""
    stream = session.events()
    return [(await asyncio.wait_for(anext(stream), 1)).payload for _ in range(count)]


async def silent(session: AgentSession) -> bool:
    """Whether ``session`` publishes nothing for a moment."""
    try:
        await asyncio.wait_for(anext(session.events()), 0.05)
    except TimeoutError:
        return True
    return False


def version(protocol_version: int) -> Hello:
    return Hello(client="orbit-tests", protocol_version=protocol_version)


async def test_nothing_is_published_before_hello() -> None:
    session = AgentSession(workspace="/work")
    assert await silent(session)
    await session.close()


async def test_hello_is_welcomed_then_a_catalog_a_snapshot_and_the_workspace_note() -> None:
    session = AgentSession(workspace="/work", verification=Verification((STEP,), Steps()))
    await greet(session)
    events = await published(session, 4)
    await session.close()

    assert events[0] == Welcome(command_id=HELLO_ID, server_version=__version__)
    assert isinstance(events[1], Catalog)
    assert isinstance(events[2], Snapshot)
    assert [step.name for step in events[2].steps] == [STEP.name]
    assert events[3] == Notice(message="Workspace: /work")
    assert nothing_queued(session)


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


OTHER_WORKSPACE = "No workspace 'other'; the workspace is default."
OTHER_SESSION = "Session 'other' isn't open; the open session is session-1."


async def test_a_misaddressed_command_before_hello_is_refused_for_its_address() -> None:
    session = AgentSession(new_id=lambda: "session-1")
    await send_as(session, "c1", AddTask(task=TaskSpec(prompt="Add a flag.")), workspace="other")
    await send_as(session, "c2", AddTask(task=TaskSpec(prompt="Add a flag.")), session="other")
    refusals = await published(session, 2)
    await session.close()

    assert refusals == [
        CommandRefused(command_id="c1", code=UNKNOWN, message=OTHER_WORKSPACE),
        CommandRefused(command_id="c2", code=WRONG_SESSION, message=OTHER_SESSION),
    ]


async def test_a_misaddressed_hello_is_refused_unwelcomed_and_a_proper_one_is_welcomed() -> None:
    session = AgentSession(workspace="/work", new_id=lambda: "session-1")
    await send_as(session, "h1", HELLO, workspace="other")
    await send_as(session, "h2", HELLO, session="other")
    refusals = await published(session, 2)
    quiet = await silent(session)
    await greet(session)
    events = await published(session, 4)
    await session.close()

    assert refusals == [
        CommandRefused(command_id="h1", code=UNKNOWN, message=OTHER_WORKSPACE),
        CommandRefused(command_id="h2", code=WRONG_SESSION, message=OTHER_SESSION),
    ]
    assert quiet
    assert events[0] == Welcome(command_id=HELLO_ID, server_version=__version__)
    assert [type(event) for event in events[1:]] == [Catalog, Snapshot, Notice]
    assert nothing_queued(session)


async def test_a_second_hello_is_refused_as_invalid() -> None:
    session = AgentSession()
    await greet(session)
    await send_as(session, "again", HELLO)
    events = await published(session, 4)
    await session.close()

    assert events[-1] == CommandRefused(
        command_id="again", code=INVALID, message=HELLO_AGAIN_MESSAGE
    )


async def test_another_protocol_version_is_refused_and_ends_the_stream() -> None:
    session = AgentSession()
    await send_as(session, "hello", version(PROTOCOL_VERSION + 1))
    await send_as(session, "add", AddTask(task=TaskSpec(prompt="Add a flag.")))
    await send_as(session, "again", HELLO)
    events = [envelope.payload async for envelope in session.events()]
    await session.close()

    message = "Protocol version 2 isn't supported; this orbit speaks version 1."
    assert events == [CommandRefused(command_id="hello", code=INCOMPATIBLE, message=message)]
    assert nothing_queued(session)


async def test_the_startup_check_and_usage_monitor_wait_for_hello() -> None:
    steps, source = Steps(True), Source(UsageReport(LIMITS))
    session, _, _ = session_for(
        verification=Verification((STEP,), steps), check_at_startup=True, usage=source
    )
    assert await silent(session)
    assert (steps.runs, source.calls) == ([], 0)

    await greet(session)
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


async def test_a_hello_whose_leftovers_fail_is_welcomed_once_and_reports_an_error() -> None:
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
    assert outcomes == [Welcome(command_id=HELLO_ID, server_version=__version__)]
    assert ErrorOccurred(code=INTERNAL, message=message) in events
    assert accepted[-1] == CommandAccepted(command_id="add")
