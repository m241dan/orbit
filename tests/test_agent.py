import asyncio
import sys
from collections.abc import AsyncIterator, Sequence
from typing import Any

from orbit.agent import (
    NO_STEPS_NOTE,
    AgentSession,
    EchoModel,
    ModelLoopRunner,
    ToolRegistry,
    Verification,
    VerificationStep,
    Workspace,
    run_step,
)
from orbit.agent.application.loop import run_turn
from orbit.agent.domain.conversation import Message, ModelResponse, ToolCall, ToolSpec
from orbit.agent.domain.verification import MAX_FIX_ATTEMPTS
from orbit.protocol import (
    AddTask,
    AssistantMessage,
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
    ToolAccess,
    ToolCallFinished,
    ToolCallStarted,
    TurnEndReason,
    TurnFinished,
    TurnStarted,
    UpdateSettings,
    VerificationChanged,
    VerificationFinished,
)


class ScriptedModel:
    def __init__(self, *responses: ModelResponse) -> None:
        self._responses = list(responses)

    async def complete(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> ModelResponse:
        return self._responses.pop(0)


class UpperTool:
    spec = ToolSpec(name="upper", description="Uppercase text.", parameters={})

    async def run(self, arguments: dict[str, Any]) -> str:
        return str(arguments["text"]).upper()


class HangingModel:
    async def complete(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> ModelResponse:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def collect(session: AgentSession) -> list[Event]:
    """Events up to the next TurnFinished, ignoring settings and busy announcements."""
    events: list[Event] = []
    async for event in session.events():
        if isinstance(event, SettingsChanged | BusyChanged):
            continue
        events.append(event)
        if isinstance(event, TurnFinished):
            return events
    return events


async def test_loop_runs_tools_until_model_stops() -> None:
    model = ScriptedModel(
        ModelResponse(text="", tool_calls=(ToolCall("c1", "upper", {"text": "hi"}),)),
        ModelResponse(text="done"),
    )
    history = [Message(role="user", content="go")]
    events = [e async for e in run_turn("t", history, model, ToolRegistry([UpperTool()]))]

    assert [type(e) for e in events] == [
        TurnStarted,
        ToolCallStarted,
        ToolCallFinished,
        AssistantMessage,
        TurnFinished,
    ]
    assert events[2] == ToolCallFinished(turn_id="t", call_id="c1", output="HI")
    assert history[-2] == Message(role="tool", content="HI", tool_call_id="c1")


async def test_loop_reports_tool_errors_to_model() -> None:
    model = ScriptedModel(
        ModelResponse(text="", tool_calls=(ToolCall("c1", "missing", {}),)),
        ModelResponse(text="ok"),
    )
    history = [Message(role="user", content="go")]
    events = [e async for e in run_turn("t", history, model, ToolRegistry())]

    finished = next(e for e in events if isinstance(e, ToolCallFinished))
    assert finished.is_error
    assert events[-1] == TurnFinished(turn_id="t", reason="completed")


async def test_loop_stops_at_max_steps() -> None:
    call = ModelResponse(text="", tool_calls=(ToolCall("c", "upper", {"text": "x"}),))
    model = ScriptedModel(call, call)
    history = [Message(role="user", content="go")]
    events = [
        e async for e in run_turn("t", history, model, ToolRegistry([UpperTool()]), max_steps=2)
    ]
    assert events[-1] == TurnFinished(turn_id="t", reason="max_steps")


async def test_session_echo_turn() -> None:
    runner = ModelLoopRunner(EchoModel())
    session = AgentSession(runner)
    await session.send(SubmitPrompt(text="hello"))
    events = await collect(session)

    assert any(isinstance(e, AssistantMessage) and e.text == "echo: hello" for e in events)
    assert isinstance(events[-1], TurnFinished) and events[-1].reason == "completed"
    assert [m.content for m in runner.history] == ["hello", "echo: hello"]


async def test_session_cancel() -> None:
    runner = ModelLoopRunner(HangingModel())
    session = AgentSession(runner)
    await session.send(SubmitPrompt(text="hello"))
    await asyncio.sleep(0)
    await session.send(CancelTurn())
    events = await collect(session)

    assert isinstance(events[-1], TurnFinished) and events[-1].reason == "cancelled"
    assert runner.history == []


async def test_session_rejects_concurrent_turn() -> None:
    session = AgentSession(ModelLoopRunner(HangingModel()))
    await session.send(SubmitPrompt(text="one"))
    await session.send(SubmitPrompt(text="two"))
    stream = session.events()
    assert isinstance(await anext(stream), SettingsChanged)
    assert await anext(stream) == BusyChanged(busy=True)
    assert isinstance(await anext(stream), ErrorOccurred)
    await session.send(CancelTurn())


class FailingModel:
    async def complete(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> ModelResponse:
        raise RuntimeError("boom")


async def test_session_cancel_without_turn_is_noop() -> None:
    session = AgentSession(ModelLoopRunner(EchoModel()))
    await session.send(CancelTurn())
    await session.send(SubmitPrompt(text="hello"))
    events = await collect(session)

    assert isinstance(events[0], TurnStarted)
    assert isinstance(events[-1], TurnFinished) and events[-1].reason == "completed"


async def test_session_reports_model_errors() -> None:
    runner = ModelLoopRunner(FailingModel())
    session = AgentSession(runner)
    await session.send(SubmitPrompt(text="hello"))
    events = await collect(session)

    assert isinstance(events[0], TurnStarted)
    turn_id = events[0].turn_id
    assert events[1:] == [
        ErrorOccurred(turn_id=turn_id, message="boom"),
        TurnFinished(turn_id=turn_id, reason="error"),
    ]
    assert runner.history == []


class RecordingModel:
    def __init__(self) -> None:
        self.tools_seen: list[list[str]] = []

    async def complete(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> ModelResponse:
        self.tools_seen.append([t.name for t in tools])
        return ModelResponse(text="ok")


class RecordingRunner:
    """Records each turn as (runner, prompt, settings) in a log shared with its new contexts.

    Turns complete, except that a prompt starting with "bad" ends in an error.
    """

    def __init__(self, log: list[tuple["RecordingRunner", str, Settings]] | None = None) -> None:
        self.log = [] if log is None else log
        self.settings_seen: list[Settings] = []

    async def run_turn(self, turn_id: str, prompt: str, settings: Settings) -> AsyncIterator[Event]:
        self.settings_seen.append(settings)
        self.log.append((self, prompt, settings))
        reason: TurnEndReason = "error" if prompt.startswith("bad") else "completed"
        yield TurnStarted(turn_id=turn_id)
        yield TurnFinished(turn_id=turn_id, reason=reason)


async def test_loop_runner_hides_tools_when_tool_access_is_off() -> None:
    model = RecordingModel()
    runner = ModelLoopRunner(model, ToolRegistry([UpperTool()]))
    accesses: tuple[ToolAccess, ...] = ("off", "restricted", "full")
    for access in accesses:
        _ = [e async for e in runner.run_turn("t", "go", Settings(tool_access=access))]
    assert model.tools_seen == [[], ["upper"], ["upper"]]


async def test_session_announces_initial_settings() -> None:
    settings = Settings(model="opus", tool_access="off")
    session = AgentSession(RecordingRunner(), settings, model_choices=("opus", "sonnet"))
    assert session.settings == settings
    assert await anext(session.events()) == SettingsChanged(
        settings=settings, model_choices=("opus", "sonnet")
    )


async def test_session_applies_updated_settings_to_next_turn() -> None:
    runner = RecordingRunner()
    session = AgentSession(runner, model_choices=("opus",))
    stream = session.events()
    assert await anext(stream) == SettingsChanged(settings=Settings(), model_choices=("opus",))

    await session.send(SubmitPrompt(text="one"))
    await collect_run(session)
    new = Settings(model="opus", tool_access="full")
    await session.send(UpdateSettings(settings=new))
    assert await anext(stream) == SettingsChanged(settings=new, model_choices=("opus",))
    await session.send(SubmitPrompt(text="two"))
    await collect_run(session)

    assert runner.settings_seen == [Settings(), new]
    assert session.settings == new


async def collect_run(session: AgentSession) -> list[Event]:
    """Events until the session goes idle, ignoring settings announcements."""
    events: list[Event] = []
    async for event in session.events():
        if isinstance(event, SettingsChanged):
            continue
        events.append(event)
        if event == BusyChanged(busy=False):
            return events
    return events


def kinds(events: list[Event]) -> list[str]:
    return [e.type for e in events]


async def test_add_and_edit_tasks_publish_the_board() -> None:
    session = AgentSession(RecordingRunner())
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


async def test_prompt_turns_are_wrapped_in_busy_notices() -> None:
    session = AgentSession(RecordingRunner())
    await session.send(SubmitPrompt(text="hi"))
    events = await collect_run(session)
    assert kinds(events) == ["busy_changed", "turn_started", "turn_finished", "busy_changed"]
    assert events[0] == BusyChanged(busy=True)


async def test_work_started_as_the_last_ends_is_not_reported_idle() -> None:
    session = AgentSession(RecordingRunner())
    await session.send(SubmitPrompt(text="one"))
    turn = session._turn
    assert turn is not None
    while not turn.done():
        await asyncio.sleep(0)
    # The first turn is done but hasn't reported idle yet when the second starts.
    await session.send(SubmitPrompt(text="two"))
    events = await collect_run(session)
    assert [e for e in events if isinstance(e, BusyChanged)] == [
        BusyChanged(busy=True),
        BusyChanged(busy=True),
        BusyChanged(busy=False),
    ]
    assert kinds(events).count("turn_finished") == 2


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
    session = AgentSession(RecordingRunner(), verification=verified((FAILING_STEP,)))
    assert session.verification == verified((FAILING_STEP,))
    stream = session.events()
    assert isinstance(await anext(stream), SettingsChanged)
    assert await anext(stream) == VerificationChanged(steps=(StepState(name="always fails"),))


async def test_verification_without_steps_notes_that_nothing_is_verified() -> None:
    session = AgentSession(RecordingRunner(), verification=verified(()))
    assert session.verification == verified(())
    stream = session.events()
    assert isinstance(await anext(stream), SettingsChanged)
    # No steps to announce, just the note.
    assert await anext(stream) == Notice(message=NO_STEPS_NOTE)
    assert session._events.empty()


async def test_conversation_turns_are_not_verified() -> None:
    session = AgentSession(ModelLoopRunner(EchoModel()), verification=verified((FAILING_STEP,)))
    await session.send(SubmitPrompt(text="hello"))
    events = await collect_run(session)

    assert not any(isinstance(e, VerificationChanged | VerificationFinished) for e in events[1:])
    await asyncio.sleep(0.01)
    assert session._events.empty()


async def test_closing_a_session_cancels_its_turn() -> None:
    session = AgentSession(ModelLoopRunner(HangingModel()))
    await session.send(SubmitPrompt(text="hi"))
    await asyncio.wait_for(session.close(), 5)
    turn = session._turn
    assert turn is not None and turn.done()
