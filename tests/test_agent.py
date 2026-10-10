import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

from orbit.agent import AgentSession, EchoModel, ModelLoopRunner, ToolRegistry
from orbit.agent.loop import run_turn
from orbit.agent.model import Message, ModelResponse, ToolCall, ToolSpec
from orbit.protocol import (
    AssistantMessage,
    CancelTurn,
    ErrorOccurred,
    Event,
    Settings,
    SettingsChanged,
    SubmitPrompt,
    ToolAccess,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
    UpdateSettings,
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
    """Events up to the next TurnFinished, ignoring settings announcements."""
    events: list[Event] = []
    async for event in session.events():
        if isinstance(event, SettingsChanged):
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
    def __init__(self) -> None:
        self.settings_seen: list[Settings] = []

    async def run_turn(self, turn_id: str, prompt: str, settings: Settings) -> AsyncIterator[Event]:
        self.settings_seen.append(settings)
        yield TurnStarted(turn_id=turn_id)
        yield TurnFinished(turn_id=turn_id, reason="completed")


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
    await collect(session)
    new = Settings(model="opus", tool_access="full")
    await session.send(UpdateSettings(settings=new))
    assert await anext(stream) == SettingsChanged(settings=new, model_choices=("opus",))
    await session.send(SubmitPrompt(text="two"))
    await collect(session)

    assert runner.settings_seen == [Settings(), new]
    assert session.settings == new
