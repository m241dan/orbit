"""Every command's outcome: accepted after the events its handling published, or refused with a
code and a message, in place of any other answer."""

import asyncio

import pytest

from orbit import __version__
from orbit.agent import AgentSession
from orbit.agent.application.planner_seat import (
    NO_PLANNER_MESSAGE,
    NOTHING_TO_RESET,
    PLANNER_BUSY_RESET,
)
from orbit.agent.application.ports import StoredSession
from orbit.agent.application.session import CONFIGURING_NOTE, NO_AGENTS_MESSAGE
from orbit.agent.domain.catalog import LANES, TRIGGERS
from orbit.protocol import (
    BUSY,
    CONFIGURE_CHECKS,
    INTERNAL,
    INVALID,
    LOCKED,
    NOT_CONFIGURED,
    PLANNER,
    SUPER_REVIEW,
    UNKNOWN,
    WRONG_SESSION,
    AddTask,
    CancelTurn,
    Command,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    ErrorOccurred,
    Event,
    FireTrigger,
    ListSessions,
    Notice,
    RemoveTask,
    ResetConversation,
    RunTasks,
    SendMessage,
    TasksChanged,
    TaskSpec,
    TurnEndReason,
    Welcome,
)
from tests.agent_fakes import (
    HELLO_ID,
    Call,
    FakeStore,
    events_until,
    greet,
    greeted,
    queued,
    session_for,
)
from tests.test_configure_key import Configurator, no_steps
from tests.test_planner import hangs

TASK = TaskSpec(prompt="Add a flag.")
OUTCOMES = (CommandAccepted, CommandRefused)


async def send_as(
    session: AgentSession, command_id: str, command: Command, /, **fields: str
) -> None:
    """Send ``command`` to ``session`` as ``command_id``, with the envelope's other ``fields``,
    greeting it first if it hasn't been."""
    await greeted(session)
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


async def answering() -> AgentSession:
    """A session whose planner is answering, and goes on until cancelled."""
    session, _, launcher = session_for()
    launcher.planner_turns = [hangs]
    await send_as(session, "plan", SendMessage(agent=PLANNER, text="Plan."))
    await events_until(session, lambda event: event == CommandAccepted(command_id="plan"))
    while not launcher.launches:
        await asyncio.sleep(0)
    return session


async def test_a_message_to_the_planner_while_it_answers_is_refused_as_busy() -> None:
    session = await answering()
    refused = await refusal(session, "c9", SendMessage(agent=PLANNER, text="More."))
    await session.close()

    assert (refused.code, refused.message) == (BUSY, "The planner is already answering.")


async def test_a_message_without_a_planner_is_refused_as_not_configured() -> None:
    session = AgentSession()
    refused = await refusal(session, "c10", SendMessage(agent=PLANNER, text="Plan."))
    await session.close()

    assert (refused.code, refused.message) == (NOT_CONFIGURED, NO_PLANNER_MESSAGE)


async def test_resetting_the_planner_while_it_answers_is_refused_as_busy() -> None:
    session = await answering()
    refused = await refusal(session, "c11", ResetConversation(agent=PLANNER))
    await session.close()

    assert (refused.code, refused.message) == (BUSY, PLANNER_BUSY_RESET)


async def test_a_reset_with_nothing_to_reset_is_noted_and_accepted() -> None:
    session, _, _ = session_for()
    await send_as(session, "c12", ResetConversation(agent=PLANNER))
    events = await outcome(session)
    await session.close()

    assert events[-2:] == [Notice(message=NOTHING_TO_RESET), CommandAccepted(command_id="c12")]


async def test_running_tasks_while_they_run_is_refused_as_busy() -> None:
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
    await send_as(session, "next", AddTask(task=TASK))
    await events_until(session, lambda event: event == CommandAccepted(command_id="next"))
    refused = await refusal(session, "c13", RunTasks())
    await session.close()

    assert (refused.code, refused.message) == (BUSY, "Tasks are already running.")


async def test_a_save_that_cannot_take_its_lock_is_an_error_not_a_refusal() -> None:
    store = FakeStore()
    store.free = False
    session = AgentSession(store=store, new_id=lambda: "session-1")
    await send_as(session, "c14", AddTask(task=TASK))
    events = await outcome(session)
    await session.close()

    message = "Another orbit has the session session-1 open, so it won't be saved."
    assert ErrorOccurred(code=LOCKED, message=message) in events
    assert events[-1] == CommandAccepted(command_id="c14")
    assert not any(isinstance(event, OUTCOMES) for event in queued(session))


async def test_a_handler_that_fails_is_refused_as_internal_and_the_session_goes_on() -> None:
    store = FakeStore()
    session = AgentSession(store=store, new_id=lambda: "session-1")
    await greeted(session)
    store.list_failure = RuntimeError("the disk is gone")
    refused = await refusal(session, "c15", ListSessions())
    store.list_failure = None
    await send_as(session, "c16", AddTask(task=TASK))
    events = await outcome(session)
    await session.close()

    message = "Orbit failed to handle ListSessions: the disk is gone"
    assert (refused.code, refused.message) == (INTERNAL, message)
    assert events[-1] == CommandAccepted(command_id="c16")


class SecondListFails(FakeStore):
    """A store whose sessions list once, for the catalog, then fail to."""

    def __init__(self) -> None:
        super().__init__()
        self.listings = 0

    async def sessions(self) -> list[StoredSession]:
        self.listings += 1
        if self.listings > 1:
            raise RuntimeError("the disk is gone")
        return await super().sessions()


async def test_a_hello_whose_startup_listing_fails_is_welcomed_once_with_an_error() -> None:
    session = AgentSession(store=SecondListFails(), new_id=lambda: "session-1", pick_session=True)
    await greet(session)
    events = queued(session)
    await send_as(session, "c18", AddTask(task=TASK))
    accepted = await outcome(session)
    await session.close()

    message = "Orbit failed to finish greeting the frontend: the disk is gone"
    assert [event for event in events if isinstance(event, (*OUTCOMES, Welcome))] == [
        Welcome(command_id=HELLO_ID, server_version=__version__)
    ]
    assert ErrorOccurred(code=INTERNAL, message=message) in events
    assert accepted[-1] == CommandAccepted(command_id="c18")


async def test_a_hello_whose_catalog_fails_is_refused_as_internal_with_no_welcome() -> None:
    store = FakeStore()
    store.list_failure = RuntimeError("the disk is gone")
    session = AgentSession(store=store, new_id=lambda: "session-1")
    await greet(session)
    events = queued(session)
    await session.close()

    message = "Orbit failed to handle Hello: the disk is gone"
    assert events == [CommandRefused(command_id=HELLO_ID, code=INTERNAL, message=message)]


async def test_a_cancelled_handler_is_cancelled_with_no_outcome() -> None:
    store = FakeStore()
    session = AgentSession(store=store, new_id=lambda: "session-1")
    await greeted(session)
    store.list_failure = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await send_as(session, "c17", ListSessions())
    store.list_failure = None
    queue = queued(session)
    await session.close()

    assert not any(isinstance(event, OUTCOMES) for event in queue)


@pytest.mark.parametrize("trigger", [entry.name for entry in TRIGGERS])
async def test_every_catalogued_trigger_is_answered_by_fire_trigger(trigger: str) -> None:
    session = AgentSession()
    await send_as(session, "c1", FireTrigger(trigger=trigger))
    events = await outcome(session)
    await session.close()

    assert not (isinstance(events[-1], CommandRefused) and events[-1].code == UNKNOWN)


@pytest.mark.parametrize("lane", LANES)
async def test_every_catalogued_lane_is_accepted_by_cancel_turn(lane: str) -> None:
    session = AgentSession()
    await send_as(session, "c1", CancelTurn(lane=lane))
    events = await outcome(session)
    await session.close()

    assert events[-1] == CommandAccepted(command_id="c1")
