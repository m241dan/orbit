"""Every command's outcome: accepted after the events its handling published, or refused with a
code and a message, in place of any other answer."""

import asyncio

import pytest

from orbit.agent import WorkspaceHost
from orbit.agent.application.planner_seat import (
    NO_PLANNER_MESSAGE,
    NOTHING_TO_RESET,
    PLANNER_BUSY_RESET,
)
from orbit.agent.application.ports import StoreFailed
from orbit.agent.application.work_turns import CONFIGURING_NOTE, NO_AGENTS_MESSAGE
from orbit.agent.domain.catalog import LANES, TRIGGERS
from orbit.agent.domain.sessions import SessionRecord
from orbit.protocol import (
    BUSY,
    CONFIGURE_CHECKS,
    INTERNAL,
    INVALID,
    LOCKED,
    NOT_CONFIGURED,
    PLANNER,
    SAVE_FAILED,
    SUPER_REVIEW,
    UNKNOWN,
    WRONG_SESSION,
    AddTask,
    CancelTurn,
    Command,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    DeleteSession,
    ErrorOccurred,
    Event,
    FireTrigger,
    Notice,
    RemoveTask,
    RenameSession,
    ResetConversation,
    RunTasks,
    SendMessage,
    TasksChanged,
    TurnEndReason,
)
from tests.agent_fakes import (
    HELLO,
    HELLO_ID,
    START,
    WORKSPACE,
    Call,
    Configurator,
    FakeStore,
    events_until,
    frontend,
    greeted,
    hangs,
    logged_with_traceback,
    no_steps,
    queued,
    session_for,
)
from tests.server_fakes import TASK

OUTCOMES = (CommandAccepted, CommandRefused)


async def send_as(
    session: WorkspaceHost, command_id: str, command: Command, /, **fields: str
) -> None:
    """Send ``command`` to ``session`` as ``command_id``, with the envelope's other ``fields``,
    greeting it first if it hasn't been."""
    await greeted(session)
    await frontend(session).send(CommandEnvelope(command_id=command_id, payload=command, **fields))


async def outcome(session: WorkspaceHost) -> list[Event]:
    """The session's events up to and including the next command's outcome."""
    return await events_until(session, lambda event: isinstance(event, OUTCOMES))


async def refusal(session: WorkspaceHost, command_id: str, command: Command) -> CommandRefused:
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
    session = WorkspaceHost()
    await send_as(session, "c1", AddTask(task=TASK))
    events = await outcome(session)
    await session.close()

    assert isinstance(events[-2], TasksChanged)
    assert events[-1] == CommandAccepted(command_id="c1")


async def test_an_unknown_trigger_is_refused_as_unknown_with_its_message() -> None:
    session = WorkspaceHost()
    refused = await refusal(session, "c2", FireTrigger(trigger="nope"))
    await session.close()

    message = "No trigger 'nope'; the triggers are super-review and configure-checks."
    assert refused == CommandRefused(command_id="c2", code=UNKNOWN, message=message)


async def test_running_tasks_without_agents_is_refused_as_not_configured() -> None:
    session = WorkspaceHost()
    refused = await refusal(session, "c3", RunTasks())
    await session.close()

    assert (refused.code, refused.message) == (NOT_CONFIGURED, NO_AGENTS_MESSAGE)


async def test_input_to_a_trigger_is_refused_as_invalid() -> None:
    session = WorkspaceHost()
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
    session = WorkspaceHost()
    await send_as(session, "c6", AddTask(task=TASK), workspace="other")
    events = await outcome(session)
    await session.close()

    message = f"No workspace 'other'; the workspace served is {WORKSPACE}."
    assert events[-1] == CommandRefused(command_id="c6", code=UNKNOWN, message=message)
    assert not any(isinstance(event, TasksChanged) for event in events)


async def test_a_command_for_a_session_not_open_is_refused_unhandled() -> None:
    session = WorkspaceHost(new_id=lambda: "session-1")
    await send_as(session, "c7", AddTask(task=TASK), session="other")
    events = await outcome(session)
    await session.close()

    message = "Session 'other' isn't open; the open session is session-1."
    assert events[-1] == CommandRefused(command_id="c7", code=WRONG_SESSION, message=message)
    assert not any(isinstance(event, TasksChanged) for event in events)


async def test_a_command_for_the_open_session_by_its_id_is_handled() -> None:
    session = WorkspaceHost(new_id=lambda: "session-1")
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


async def answering() -> WorkspaceHost:
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
    session = WorkspaceHost()
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
    session = WorkspaceHost(store=store, new_id=lambda: "session-1")
    await send_as(session, "c14", AddTask(task=TASK))
    events = await outcome(session)
    await session.close()

    message = "Another orbit has the session session-1 open, so it won't be saved."
    assert ErrorOccurred(code=LOCKED, message=message) in events
    assert events[-1] == CommandAccepted(command_id="c14")
    assert not any(isinstance(event, OUTCOMES) for event in queued(session))


async def test_a_handler_that_fails_is_refused_as_internal_and_the_session_goes_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeStore()
    session = WorkspaceHost(store=store, new_id=lambda: "session-1")
    await greeted(session)
    store.list_failure = ValueError("the disk is gone")
    refused = await refusal(session, "c15", DeleteSession(session_id="other"))
    store.list_failure = None
    await send_as(session, "c16", AddTask(task=TASK))
    events = await outcome(session)
    await session.close()

    message = "Orbit failed to handle DeleteSession: the disk is gone"
    assert (refused.code, refused.message) == (INTERNAL, message)
    assert logged_with_traceback(caplog.records, message)
    assert events[-1] == CommandAccepted(command_id="c16")


@pytest.mark.parametrize(
    "command",
    [RenameSession(session_id="other", name="Renamed"), DeleteSession(session_id="other")],
)
async def test_a_change_while_sessions_cannot_be_listed_reports_it_once_and_is_answered(
    command: RenameSession | DeleteSession,
) -> None:
    store = FakeStore()
    store.saved = [SessionRecord(id="other", name="other", created=START, last_active=START)]
    session = WorkspaceHost(store=store, new_id=lambda: "session-1")
    await greeted(session)
    store.list_failure = StoreFailed("Couldn't list the sessions in orbit.db: busy")
    await send_as(session, "c20", command)
    events = await outcome(session)
    await session.close()

    message = "Couldn't list the sessions in orbit.db: busy"
    errors = [e for e in events if isinstance(e, ErrorOccurred)]
    assert errors == [ErrorOccurred(code=SAVE_FAILED, message=message)]
    assert events[-1] == CommandAccepted(command_id="c20")


async def test_a_hello_whose_catalog_fails_is_refused_as_internal_with_no_welcome() -> None:
    store = FakeStore()
    store.list_failure = ValueError("the disk is gone")
    session = WorkspaceHost(store=store, new_id=lambda: "session-1")
    await frontend(session).send(CommandEnvelope(command_id=HELLO_ID, payload=HELLO))
    events = queued(session)
    await session.close()

    message = "Orbit failed to handle Hello: the disk is gone"
    assert events == [CommandRefused(command_id=HELLO_ID, code=INTERNAL, message=message)]


async def test_a_cancelled_handler_is_cancelled_with_no_outcome() -> None:
    store = FakeStore()
    session = WorkspaceHost(store=store, new_id=lambda: "session-1")
    await greeted(session)
    queued(session)  # The greeting's.
    store.list_failure = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await send_as(session, "c17", DeleteSession(session_id="other"))
    store.list_failure = None
    queue = queued(session)
    await session.close()

    assert not any(isinstance(event, OUTCOMES) for event in queue)


@pytest.mark.parametrize("trigger", [entry.name for entry in TRIGGERS])
async def test_every_catalogued_trigger_is_run_by_fire_trigger(trigger: str) -> None:
    session = WorkspaceHost()
    refused = await refusal(session, "c1", FireTrigger(trigger=trigger))
    await session.close()

    # With no agents every trigger's run refuses; one without a run would be INTERNAL.
    assert refused.code == NOT_CONFIGURED


@pytest.mark.parametrize("lane", LANES)
async def test_every_catalogued_lane_is_accepted_by_cancel_turn(lane: str) -> None:
    session = WorkspaceHost()
    await send_as(session, "c1", CancelTurn(lane=lane))
    events = await outcome(session)
    await session.close()

    assert events[-1] == CommandAccepted(command_id="c1")
