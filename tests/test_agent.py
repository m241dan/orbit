import asyncio
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from orbit.agent import AgentSession, EchoModel, ModelLoopRunner, ToolRegistry
from orbit.agent.loop import run_turn
from orbit.agent.model import Message, ModelResponse, ToolCall, ToolSpec
from orbit.agent.verify import VerificationStep
from orbit.protocol import (
    AssistantMessage,
    CancelTurn,
    ErrorOccurred,
    Event,
    RunTasks,
    Settings,
    SettingsChanged,
    SubmitPrompt,
    TaskFinished,
    TaskInserted,
    TaskSpec,
    TaskStarted,
    ToolAccess,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
    UpdateSettings,
    VerificationConfigured,
    VerificationFinished,
    VerificationStarted,
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
    """Records each turn as (runner, prompt, settings) in a log shared with its new contexts."""

    def __init__(self, log: list[tuple["RecordingRunner", str, Settings]] | None = None) -> None:
        self.log = [] if log is None else log
        self.settings_seen: list[Settings] = []

    def new_context(self) -> "RecordingRunner":
        return RecordingRunner(self.log)

    async def run_turn(self, turn_id: str, prompt: str, settings: Settings) -> AsyncIterator[Event]:
        self.settings_seen.append(settings)
        self.log.append((self, prompt, settings))
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


async def collect_tasks(session: AgentSession, count: int) -> list[Event]:
    """Events until task ``count - 1`` finishes or a task is cancelled."""
    events: list[Event] = []
    async for event in session.events():
        if isinstance(event, SettingsChanged):
            continue
        events.append(event)
        if isinstance(event, TaskFinished) and (
            event.index == count - 1 or event.reason == "cancelled"
        ):
            return events
    return events


def kinds(events: list[Event]) -> list[str]:
    return [e.type for e in events]


async def test_run_tasks_runs_each_in_a_new_context_with_its_settings() -> None:
    runner = RecordingRunner()
    session = AgentSession(runner, Settings(model="sonnet"))
    first = TaskSpec(prompt="one", settings=Settings(model="opus", tool_access="full"))
    second = TaskSpec(prompt="two", settings=Settings(tool_access="off"))
    await session.send(RunTasks(tasks=(first, second)))
    events = await collect_tasks(session, 2)

    assert kinds(events) == [
        "task_started",
        "turn_started",
        "turn_finished",
        "task_finished",
        "task_started",
        "turn_started",
        "turn_finished",
        "task_finished",
    ]
    assert events[0] == TaskStarted(index=0)
    assert events[3] == TaskFinished(index=0, reason="completed")
    assert events[7] == TaskFinished(index=1, reason="completed")

    runners = [entry[0] for entry in runner.log]
    assert [(p, s) for _, p, s in runner.log] == [
        ("one", first.settings),
        ("two", second.settings),
    ]
    assert runner not in runners
    assert runners[0] is not runners[1]
    # The session's own settings are untouched.
    assert session.settings == Settings(model="sonnet")


async def test_run_tasks_leaves_the_conversation_alone() -> None:
    runner = ModelLoopRunner(EchoModel())
    session = AgentSession(runner)
    await session.send(SubmitPrompt(text="hello"))
    await collect(session)
    await session.send(RunTasks(tasks=(TaskSpec(prompt="task"),)))
    events = await collect_tasks(session, 1)

    assert any(isinstance(e, AssistantMessage) and e.text == "echo: task" for e in events)
    assert [m.content for m in runner.history] == ["hello", "echo: hello"]


async def test_cancel_stops_the_remaining_tasks() -> None:
    session = AgentSession(ModelLoopRunner(HangingModel()))
    await session.send(RunTasks(tasks=(TaskSpec(prompt="one"), TaskSpec(prompt="two"))))
    await asyncio.sleep(0)
    await session.send(CancelTurn())
    events = await collect_tasks(session, 2)

    assert kinds(events) == ["task_started", "turn_started", "turn_finished", "task_finished"]
    assert events[-1] == TaskFinished(index=0, reason="cancelled")
    await asyncio.sleep(0.01)
    assert session._events.empty()


async def test_failed_task_does_not_stop_the_next() -> None:
    session = AgentSession(ModelLoopRunner(FailingModel()))
    await session.send(RunTasks(tasks=(TaskSpec(prompt="one"), TaskSpec(prompt="two"))))
    events = await collect_tasks(session, 2)

    finished = [e for e in events if isinstance(e, TaskFinished)]
    assert finished == [
        TaskFinished(index=0, reason="error"),
        TaskFinished(index=1, reason="error"),
    ]
    assert any(isinstance(e, ErrorOccurred) and e.message == "boom" for e in events)


async def test_run_tasks_rejected_while_a_turn_runs() -> None:
    session = AgentSession(ModelLoopRunner(HangingModel()))
    await session.send(SubmitPrompt(text="one"))
    await session.send(RunTasks(tasks=(TaskSpec(prompt="task"),)))
    stream = session.events()
    assert isinstance(await anext(stream), SettingsChanged)
    assert await anext(stream) == ErrorOccurred(message="A turn is already running.")
    await session.send(CancelTurn())


async def test_loop_runner_new_context_has_empty_history_and_same_model() -> None:
    model = RecordingModel()
    runner = ModelLoopRunner(model, ToolRegistry([UpperTool()]), max_steps=3)
    _ = [e async for e in runner.run_turn("t", "go", Settings())]
    fresh = runner.new_context()
    assert fresh.history == []
    _ = [e async for e in fresh.run_turn("t", "again", Settings())]
    assert [m.content for m in fresh.history] == ["again", "ok"]
    assert model.tools_seen == [["upper"], ["upper"]]


def file_exists_step(path: Path, name: str = "check") -> VerificationStep:
    """Passes once ``path`` exists."""
    code = f"import os, sys; sys.exit(0 if os.path.exists({str(path)!r}) else 1)"
    return VerificationStep(name=name, command=(sys.executable, "-c", code))


FAILING_STEP = VerificationStep(
    name="always fails", command=(sys.executable, "-c", "print('nope'); raise SystemExit(1)")
)


class FixingRunner(RecordingRunner):
    """Creates ``path`` when asked to fix something, so a file_exists_step then passes."""

    def __init__(
        self, path: Path, log: list[tuple[RecordingRunner, str, Settings]] | None = None
    ) -> None:
        super().__init__(log)
        self.path = path

    def new_context(self) -> "FixingRunner":
        return FixingRunner(self.path, self.log)

    async def run_turn(self, turn_id: str, prompt: str, settings: Settings) -> AsyncIterator[Event]:
        if prompt.startswith("Fix"):
            self.path.touch()
        async for event in super().run_turn(turn_id, prompt, settings):
            yield event


async def test_session_announces_verification_steps() -> None:
    session = AgentSession(RecordingRunner(), verification_steps=(FAILING_STEP,))
    assert session.verification_steps == (FAILING_STEP,)
    stream = session.events()
    assert isinstance(await anext(stream), SettingsChanged)
    assert await anext(stream) == VerificationConfigured(steps=("always fails",))


async def test_passing_verification_marks_the_task_verified(tmp_path: Path) -> None:
    (tmp_path / "ok").touch()
    session = AgentSession(
        RecordingRunner(), verification_steps=(file_exists_step(tmp_path / "ok"),)
    )
    await session.send(RunTasks(tasks=(TaskSpec(prompt="one"),)))
    events = await collect_tasks(session, 1)

    assert kinds(events) == [
        "verification_configured",
        "task_started",
        "turn_started",
        "turn_finished",
        "verification_started",
        "verification_finished",
        "task_finished",
    ]
    assert events[4:] == [
        VerificationStarted(index=0, step=0),
        VerificationFinished(index=0, step=0, passed=True),
        TaskFinished(index=0, reason="completed", verified=True),
    ]


async def test_failed_verification_inserts_a_fix_task_after_the_task(tmp_path: Path) -> None:
    fixed = tmp_path / "fixed"
    runner = FixingRunner(fixed)
    session = AgentSession(runner, verification_steps=(file_exists_step(fixed),))
    settings = Settings(model="opus", tool_access="full")
    tasks = (TaskSpec(prompt="one", settings=settings), TaskSpec(prompt="two"))
    await session.send(RunTasks(tasks=tasks))
    events = await collect_tasks(session, 3)

    inserted = next(e for e in events if isinstance(e, TaskInserted))
    assert inserted.index == 1
    assert inserted.task.prompt.startswith("Fix the failed verification step: check")
    # The fix runs with the failed task's settings.
    assert inserted.task.settings == settings

    # The insert is announced before the failed task finishes, then the fix runs next.
    after_first = [e for e in events if not isinstance(e, TurnStarted | TurnFinished)][1:]
    assert after_first[:6] == [
        TaskStarted(index=0),
        VerificationStarted(index=0, step=0),
        VerificationFinished(index=0, step=0, passed=False),
        inserted,
        TaskFinished(index=0, reason="completed", verified=False),
        TaskStarted(index=1),
    ]
    finished = [e for e in events if isinstance(e, TaskFinished)]
    assert finished == [
        TaskFinished(index=0, reason="completed", verified=False),
        TaskFinished(index=1, reason="completed", verified=True),
        TaskFinished(index=2, reason="completed", verified=True),
    ]
    assert [p for _, p, _ in runner.log] == ["one", inserted.task.prompt, "two"]
    runners = [entry[0] for entry in runner.log]
    assert len({id(r) for r in runners}) == 3


async def test_verification_stops_at_the_first_failing_step(tmp_path: Path) -> None:
    session = AgentSession(
        RecordingRunner(),
        verification_steps=(FAILING_STEP, file_exists_step(tmp_path)),
        max_fix_attempts=0,
    )
    await session.send(RunTasks(tasks=(TaskSpec(prompt="one"),)))
    events = await collect_tasks(session, 1)

    assert [e for e in events if isinstance(e, VerificationStarted)] == [
        VerificationStarted(index=0, step=0)
    ]


async def test_fix_tasks_stop_after_max_attempts() -> None:
    runner = RecordingRunner()
    session = AgentSession(runner, verification_steps=(FAILING_STEP,), max_fix_attempts=2)
    await session.send(RunTasks(tasks=(TaskSpec(prompt="one"), TaskSpec(prompt="two"))))
    events = await collect_tasks(session, 6)

    assert [e.index for e in events if isinstance(e, TaskInserted)] == [1, 2, 4, 5]
    # Each original task gets its own two attempts, then the batch moves on.
    prompts = [p for _, p, _ in runner.log]
    assert [p.split("\n")[0] for p in prompts] == [
        "one",
        "Fix the failed verification step: always fails",
        "Fix the failed verification step: always fails",
        "two",
        "Fix the failed verification step: always fails",
        "Fix the failed verification step: always fails",
    ]
    errors = [e for e in events if isinstance(e, ErrorOccurred)]
    assert (
        errors
        == [
            ErrorOccurred(
                message="Verification step 'always fails' still fails after 2 fix attempts."
            )
        ]
        * 2
    )
    assert "nope" in prompts[1]


async def test_tasks_that_do_not_complete_are_not_verified() -> None:
    session = AgentSession(ModelLoopRunner(FailingModel()), verification_steps=(FAILING_STEP,))
    await session.send(RunTasks(tasks=(TaskSpec(prompt="one"),)))
    events = await collect_tasks(session, 1)

    assert not any(isinstance(e, VerificationStarted | TaskInserted) for e in events)
    assert events[-1] == TaskFinished(index=0, reason="error")


async def test_cancel_during_verification_cancels_the_task_and_batch() -> None:
    slow = VerificationStep(
        name="slow", command=(sys.executable, "-c", "import time; time.sleep(60)")
    )
    session = AgentSession(RecordingRunner(), verification_steps=(slow,))
    await session.send(RunTasks(tasks=(TaskSpec(prompt="one"), TaskSpec(prompt="two"))))
    events: list[Event] = []
    async for event in session.events():
        events.append(event)
        if isinstance(event, VerificationStarted):
            break
    await session.send(CancelTurn())
    events = await asyncio.wait_for(collect_tasks(session, 2), timeout=5)

    assert events == [TaskFinished(index=0, reason="cancelled")]
    await asyncio.sleep(0.01)
    assert session._events.empty()


async def test_conversation_turns_are_not_verified() -> None:
    session = AgentSession(ModelLoopRunner(EchoModel()), verification_steps=(FAILING_STEP,))
    await session.send(SubmitPrompt(text="hello"))
    events = await collect(session)

    assert not any(isinstance(e, VerificationStarted) for e in events)
    await asyncio.sleep(0.01)
    assert session._events.empty()
