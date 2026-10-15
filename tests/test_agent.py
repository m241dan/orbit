"""The session's own business: settings, prompts to the planner, and the task list."""

import asyncio
import sys
from collections.abc import Sequence

from orbit.agent import (
    NO_STEPS_NOTE,
    AgentSession,
    Verification,
    VerificationStep,
    Workspace,
    run_step,
)
from orbit.agent.domain.verification import MAX_FIX_ATTEMPTS
from orbit.protocol import (
    AddTask,
    BusyChanged,
    CancelTurn,
    EditTask,
    ErrorOccurred,
    Event,
    Notice,
    Settings,
    SettingsChanged,
    StepState,
    SubmitPrompt,
    TasksChanged,
    TaskSpec,
    TaskState,
    TurnEndReason,
    TurnFinished,
    TurnStarted,
    UpdateSettings,
    VerificationChanged,
    VerificationFinished,
)
from tests.agent_fakes import Call, session_for

PLANNER_IDLE = BusyChanged(busy=False, lane="planner")


async def chats(call: Call) -> TurnEndReason:
    return "completed"


async def fails(call: Call) -> TurnEndReason:
    raise RuntimeError("boom")


async def collect_run(session: AgentSession) -> list[Event]:
    """Events until the planner goes idle, ignoring settings announcements."""
    events: list[Event] = []
    async for event in session.events():
        if isinstance(event, SettingsChanged):
            continue
        events.append(event)
        if event == PLANNER_IDLE:
            return events
    return events


def turns(events: list[Event]) -> list[Event]:
    """The turns' starts and ends, and the busy notices around them."""
    kinds = TurnStarted | TurnFinished | BusyChanged | ErrorOccurred
    return [e for e in events if isinstance(e, kinds)]


async def test_a_prompt_starts_a_turn_of_the_planner() -> None:
    session, _, launcher = session_for()
    launcher.planner_turns = [chats]
    await session.send(SubmitPrompt(text="hello"))
    events = await asyncio.wait_for(collect_run(session), 5)

    [launch] = launcher.launches  # The planner's launcher was asked to converse.
    assert launch.agent.role == "planner" and launch.prompt.endswith("\n\nhello")
    assert [e.type for e in turns(events)] == [
        "busy_changed",
        "turn_started",
        "turn_finished",
        "busy_changed",
    ]
    assert events[0] == BusyChanged(busy=True, lane="planner")
    assert any(isinstance(e, TurnFinished) and e.reason == "completed" for e in events)


async def test_a_prompt_without_a_planner_is_refused() -> None:
    session = AgentSession()
    await session.send(SubmitPrompt(text="hello"))
    stream = session.events()
    assert isinstance(await anext(stream), SettingsChanged)
    assert await anext(stream) == ErrorOccurred(message="Planning needs agents and a planner role.")
    assert await anext(stream) == PLANNER_IDLE
    assert session._events.empty()


async def test_session_reports_the_planners_errors() -> None:
    session, _, launcher = session_for()
    launcher.planner_turns = [fails]
    await session.send(SubmitPrompt(text="hello"))
    events = turns(await asyncio.wait_for(collect_run(session), 5))

    started = events[1]
    assert isinstance(started, TurnStarted)
    turn_id = started.turn_id
    assert events[2:] == [
        ErrorOccurred(turn_id=turn_id, message="boom"),
        TurnFinished(turn_id=turn_id, reason="error"),
        PLANNER_IDLE,
    ]


async def test_cancel_without_a_turn_is_a_noop() -> None:
    session, _, launcher = session_for()
    launcher.planner_turns = [chats]
    await session.send(CancelTurn(lane="planner"))
    await session.send(SubmitPrompt(text="hello"))
    events = await asyncio.wait_for(collect_run(session), 5)
    assert any(isinstance(e, TurnFinished) and e.reason == "completed" for e in events)


async def test_session_announces_initial_settings() -> None:
    settings = Settings(model="opus", tool_access="off")
    session = AgentSession(settings, model_choices=("opus", "sonnet"))
    assert session.settings == settings
    assert await anext(session.events()) == SettingsChanged(
        settings=settings, model_choices=("opus", "sonnet")
    )


async def test_the_planner_takes_the_model_from_updated_settings() -> None:
    session, _, launcher = session_for()
    launcher.planner_turns = [chats, chats]
    stream = session.events()
    assert await anext(stream) == SettingsChanged(settings=Settings())

    await session.send(SubmitPrompt(text="one"))
    await asyncio.wait_for(collect_run(session), 5)
    new = Settings(model="opus", tool_access="full")
    await session.send(UpdateSettings(settings=new))
    assert await anext(stream) == SettingsChanged(settings=new)
    await session.send(SubmitPrompt(text="two"))
    await asyncio.wait_for(collect_run(session), 5)

    assert [launch.settings.model for launch in launcher.launches] == [None, "opus"]
    # Tool access sets new tasks' default; the planner keeps its role's.
    assert len({launch.settings.tool_access for launch in launcher.launches}) == 1
    assert session.settings == new


async def test_add_and_edit_tasks_publish_the_board() -> None:
    session = AgentSession()
    stream = session.events()
    assert isinstance(await anext(stream), SettingsChanged)

    await session.send(AddTask(task=TaskSpec(prompt="one")))
    [added] = (await anext(stream)).tasks  # type: ignore[union-attr]
    assert added == TaskState(id=added.id, task=TaskSpec(prompt="one"))

    edited = TaskSpec(prompt="one!", settings=Settings(tool_access="off"))
    await session.send(EditTask(task_id=added.id, task=edited))
    assert await anext(stream) == TasksChanged(tasks=(TaskState(id=added.id, task=edited),))

    await session.send(EditTask(task_id="nope", task=edited))
    assert await anext(stream) == ErrorOccurred(message="No task with id 'nope'.")


async def test_a_turn_started_as_the_last_ends_is_not_reported_idle() -> None:
    session, _, launcher = session_for()
    launcher.planner_turns = [chats, chats]
    await session.send(SubmitPrompt(text="one"))
    turn = session._planner_turn
    assert turn is not None
    while not turn.done():
        await asyncio.sleep(0)
    # The first turn is done but hasn't reported idle yet when the second starts.
    await session.send(SubmitPrompt(text="two"))
    events = await asyncio.wait_for(collect_run(session), 5)
    assert [e for e in events if isinstance(e, BusyChanged)] == [
        BusyChanged(busy=True, lane="planner"),
        BusyChanged(busy=True, lane="planner"),
        PLANNER_IDLE,
    ]
    assert [e.type for e in events].count("turn_finished") == 2


def verified(
    steps: Sequence[VerificationStep],
    max_fix_attempts: int = MAX_FIX_ATTEMPTS,
    workspace: Workspace | None = None,
) -> Verification:
    """Verification that runs ``steps`` as real processes."""
    return Verification(tuple(steps), run_step, workspace, max_fix_attempts)


FAILING_STEP = VerificationStep(
    name="always fails", command=(sys.executable, "-c", "print('nope'); raise SystemExit(1)")
)


async def test_session_announces_verification_steps() -> None:
    session = AgentSession(verification=verified((FAILING_STEP,)))
    assert session.verification == verified((FAILING_STEP,))
    stream = session.events()
    assert isinstance(await anext(stream), SettingsChanged)
    assert await anext(stream) == VerificationChanged(steps=(StepState(name="always fails"),))


async def test_verification_without_steps_notes_that_nothing_is_verified() -> None:
    session = AgentSession(verification=verified(()))
    assert session.verification == verified(())
    stream = session.events()
    assert isinstance(await anext(stream), SettingsChanged)
    # No steps to announce, just the note.
    assert await anext(stream) == Notice(message=NO_STEPS_NOTE)
    assert session._events.empty()


async def test_session_notes_its_workspace_at_startup() -> None:
    session = AgentSession(workspace="/code/project")
    stream = session.events()
    assert isinstance(await anext(stream), SettingsChanged)
    assert await anext(stream) == Notice(message="Workspace: /code/project")
    assert session._events.empty()


async def test_planner_turns_are_not_verified() -> None:
    session, _, launcher = session_for(verification=verified((FAILING_STEP,)))
    launcher.planner_turns = [chats]
    await session.send(SubmitPrompt(text="hello"))
    events = await asyncio.wait_for(collect_run(session), 5)

    assert not any(isinstance(e, VerificationChanged | VerificationFinished) for e in events[1:])
    await asyncio.sleep(0.01)
    assert session._events.empty()
