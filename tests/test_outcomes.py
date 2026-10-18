"""Every command's outcome: accepted after the events its handling published, or refused with a
code and a message, in place of any other answer."""

import asyncio

from orbit.agent import AgentSession
from orbit.agent.application.session import CONFIGURING_NOTE, NO_AGENTS_MESSAGE
from orbit.protocol import (
    BUSY,
    CONFIGURE_CHECKS,
    INVALID,
    NOT_CONFIGURED,
    SUPER_REVIEW,
    UNKNOWN,
    WRONG_SESSION,
    AddTask,
    Command,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    ErrorOccurred,
    Event,
    FireTrigger,
    Notice,
    RemoveTask,
    RunTasks,
    TasksChanged,
    TaskSpec,
    TurnEndReason,
)
from tests.agent_fakes import Call, events_until, queued, session_for
from tests.test_configure_key import Configurator, no_steps

TASK = TaskSpec(prompt="Add a flag.")
OUTCOMES = (CommandAccepted, CommandRefused)


async def send_as(
    session: AgentSession, command_id: str, command: Command, /, **fields: str
) -> None:
    """Send ``command`` to ``session`` as ``command_id``, with the envelope's other ``fields``."""
    await session.send(CommandEnvelope(command_id=command_id, payload=command, **fields))


async def outcome(session: AgentSession) -> list[Event]:
    """The session's events up to and including the next command's outcome."""
    return await events_until(session, lambda event: isinstance(event, OUTCOMES))


async def refusal(session: AgentSession, command_id: str, command: Command) -> CommandRefused:
    """What ``session`` refused ``command`` with, sent as ``command_id``; checks that it's the
    only answer, with no error and no other outcome after it."""
    await send_as(session, command_id, command)
    events = await outcome(session)
    refused = events[-1]
    assert isinstance(refused, CommandRefused)
    assert refused.command_id == command_id
    assert not any(isinstance(event, ErrorOccurred | CommandAccepted) for event in events)
    assert not any(isinstance(event, OUTCOMES) for event in queued(session))
    return refused


async def test_an_accepted_command_is_answered_after_the_events_it_published() -> None:
    session = AgentSession()
    await send_as(session, "c1", AddTask(task=TASK))
    events = await outcome(session)
    await session.close()

    assert isinstance(events[-2], TasksChanged)
    assert events[-1] == CommandAccepted(command_id="c1")


async def test_an_unknown_trigger_is_refused_as_unknown_with_its_message() -> None:
    session = AgentSession()
    refused = await refusal(session, "c2", FireTrigger(trigger="nope"))
    await session.close()

    message = "No trigger 'nope'; the triggers are super-review and configure-checks."
    assert refused == CommandRefused(command_id="c2", code=UNKNOWN, message=message)


async def test_running_tasks_without_agents_is_refused_as_not_configured() -> None:
    session = AgentSession()
    refused = await refusal(session, "c3", RunTasks())
    await session.close()

    assert (refused.code, refused.message) == (NOT_CONFIGURED, NO_AGENTS_MESSAGE)


async def test_input_to_a_trigger_is_refused_as_invalid() -> None:
    session = AgentSession()
    refused = await refusal(session, "c4", FireTrigger(trigger=SUPER_REVIEW, input={"x": "1"}))
    await session.close()

    assert (refused.code, refused.message) == (INVALID, "The super-review trigger takes no input.")


async def test_removing_a_running_task_is_refused_as_busy() -> None:
    started = asyncio.Event()

    async def waits(call: Call) -> TurnEndReason:
        await call("get_task")
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    session, _, _ = session_for(waits)
    await send_as(session, "add", AddTask(task=TASK))
    await send_as(session, "run", RunTasks())
    await asyncio.wait_for(started.wait(), 5)
    await events_until(session, lambda event: event == CommandAccepted(command_id="run"))
    refused = await refusal(session, "c5", RemoveTask(task_id="1"))
    await session.close()

    assert (refused.code, refused.message) == (BUSY, "Task '1' can't be removed while it runs.")


async def test_a_command_for_another_workspace_is_refused_unhandled() -> None:
    session = AgentSession()
    await send_as(session, "c6", AddTask(task=TASK), workspace="other")
    events = await outcome(session)
    await session.close()

    message = "No workspace 'other'; the workspace is default."
    assert events[-1] == CommandRefused(command_id="c6", code=UNKNOWN, message=message)
    assert not any(isinstance(event, TasksChanged) for event in events)


async def test_a_command_for_a_session_not_open_is_refused_unhandled() -> None:
    session = AgentSession(new_id=lambda: "session-1")
    await send_as(session, "c7", AddTask(task=TASK), session="other")
    events = await outcome(session)
    await session.close()

    message = "Session 'other' isn't open; the open session is session-1."
    assert events[-1] == CommandRefused(command_id="c7", code=WRONG_SESSION, message=message)
    assert not any(isinstance(event, TasksChanged) for event in events)


async def test_a_command_for_the_open_session_by_its_id_is_handled() -> None:
    session = AgentSession(new_id=lambda: "session-1")
    await send_as(session, "c8", AddTask(task=TASK), session="session-1")
    events = await outcome(session)
    await session.close()

    assert isinstance(events[-2], TasksChanged)
    assert events[-1] == CommandAccepted(command_id="c8")


async def test_a_configurator_already_queued_is_noted_and_the_command_accepted() -> None:
    session, _, _ = session_for(Configurator(), verification=no_steps())
    await send_as(session, "first", FireTrigger(trigger=CONFIGURE_CHECKS))
    await send_as(session, "second", FireTrigger(trigger=CONFIGURE_CHECKS))
    accepted = CommandAccepted(command_id="second")
    events = await events_until(session, lambda event: event == accepted)
    await session.close()

    noted = events.index(Notice(message=CONFIGURING_NOTE))
    assert noted > events.index(CommandAccepted(command_id="first"))
    assert not any(isinstance(event, CommandRefused) for event in events)
