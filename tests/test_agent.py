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
    AddTask,
    AssistantMessage,
    BusyChanged,
    CancelTurn,
    EditTask,
    ErrorOccurred,
    Event,
    RunTasks,
    Settings,
    SettingsChanged,
    StepState,
    SubmitPrompt,
    TasksChanged,
    TaskSpec,
    TaskStarted,
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

    def new_context(self) -> "RecordingRunner":
        return RecordingRunner(self.log)

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


async def add_tasks(session: AgentSession, *tasks: TaskSpec) -> list[str]:
    """Queue ``tasks``; returns their ids and drains the board updates."""
    stream = session.events()
    for task in tasks:
        await session.send(AddTask(task=task))
    board: tuple[TaskState, ...] = ()
    while len(board) < len(tasks):
        event = await anext(stream)
        if isinstance(event, TasksChanged):
            board = event.tasks
    return [t.id for t in board]


def kinds(events: list[Event]) -> list[str]:
    return [e.type for e in events]


def last_board(events: list[Event]) -> tuple[TaskState, ...]:
    return [e for e in events if isinstance(e, TasksChanged)][-1].tasks


def statuses(events: list[Event]) -> list[str]:
    return [t.status for t in last_board(events)]


def step_statuses(events: list[Event]) -> list[str]:
    last = [e for e in events if isinstance(e, VerificationChanged)][-1]
    return [s.status for s in last.steps]


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


async def test_run_tasks_runs_each_in_a_new_context_with_its_settings() -> None:
    runner = RecordingRunner()
    session = AgentSession(runner, Settings(model="sonnet"))
    first = TaskSpec(prompt="one", settings=Settings(model="opus", tool_access="full"))
    second = TaskSpec(prompt="two", settings=Settings(tool_access="off"))
    ids = await add_tasks(session, first, second)
    await session.send(RunTasks())
    events = await collect_run(session)

    assert kinds(events) == [
        "busy_changed",
        "tasks_changed",
        "task_started",
        "turn_started",
        "turn_finished",
        "tasks_changed",
        "tasks_changed",
        "task_started",
        "turn_started",
        "turn_finished",
        "tasks_changed",
        "busy_changed",
    ]
    assert events[1] == TasksChanged(
        tasks=(
            TaskState(id=ids[0], task=first, status="running"),
            TaskState(id=ids[1], task=second),
        )
    )
    assert events[2] == TaskStarted(task_id=ids[0])
    assert events[7] == TaskStarted(task_id=ids[1])
    assert statuses(events) == ["completed", "completed"]

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
    await collect_run(session)
    await add_tasks(session, TaskSpec(prompt="task"))
    await session.send(RunTasks())
    events = await collect_run(session)

    assert any(isinstance(e, AssistantMessage) and e.text == "echo: task" for e in events)
    assert [m.content for m in runner.history] == ["hello", "echo: hello"]


async def test_running_again_runs_only_tasks_that_did_not_finish() -> None:
    runner = RecordingRunner()
    session = AgentSession(runner)
    await add_tasks(session, TaskSpec(prompt="one"), TaskSpec(prompt="bad"))
    await session.send(RunTasks())
    assert statuses(await collect_run(session)) == ["completed", "error"]

    await session.send(RunTasks())
    assert statuses(await collect_run(session)) == ["completed", "error"]
    assert [p for _, p, _ in runner.log] == ["one", "bad", "bad"]


async def test_run_tasks_does_nothing_when_nothing_is_runnable() -> None:
    session = AgentSession(RecordingRunner())
    await session.send(RunTasks())
    assert isinstance(await anext(session.events()), SettingsChanged)
    assert session._events.empty()


async def test_cancel_stops_the_remaining_tasks() -> None:
    session = AgentSession(ModelLoopRunner(HangingModel()))
    await add_tasks(session, TaskSpec(prompt="one"), TaskSpec(prompt="two"))
    await session.send(RunTasks())
    await asyncio.sleep(0)
    await session.send(CancelTurn())
    events = await collect_run(session)

    assert statuses(events) == ["cancelled", "pending"]
    assert [e for e in events if isinstance(e, TaskStarted)] == events[2:3]
    await asyncio.sleep(0.01)
    assert session._events.empty()


async def test_failed_task_does_not_stop_the_next() -> None:
    session = AgentSession(ModelLoopRunner(FailingModel()))
    await add_tasks(session, TaskSpec(prompt="one"), TaskSpec(prompt="two"))
    await session.send(RunTasks())
    events = await collect_run(session)

    assert statuses(events) == ["error", "error"]
    assert any(isinstance(e, ErrorOccurred) and e.message == "boom" for e in events)


async def test_run_tasks_rejected_while_a_turn_runs() -> None:
    session = AgentSession(ModelLoopRunner(HangingModel()))
    await add_tasks(session, TaskSpec(prompt="task"))
    await session.send(SubmitPrompt(text="one"))
    await session.send(RunTasks())
    stream = session.events()
    assert await anext(stream) == BusyChanged(busy=True)
    assert await anext(stream) == ErrorOccurred(message="A turn is already running.")
    await session.send(CancelTurn())


class GatedRunner(RecordingRunner):
    """Turns wait for ``gate`` before finishing."""

    def __init__(
        self,
        gate: asyncio.Event,
        log: list[tuple[RecordingRunner, str, Settings]] | None = None,
    ) -> None:
        super().__init__(log)
        self.gate = gate

    def new_context(self) -> "GatedRunner":
        return GatedRunner(self.gate, self.log)

    async def run_turn(self, turn_id: str, prompt: str, settings: Settings) -> AsyncIterator[Event]:
        await self.gate.wait()
        async for event in super().run_turn(turn_id, prompt, settings):
            yield event


async def test_tasks_added_during_a_run_join_it_and_running_ones_cannot_be_edited() -> None:
    gate = asyncio.Event()
    runner = GatedRunner(gate)
    session = AgentSession(runner)
    [one] = await add_tasks(session, TaskSpec(prompt="one"))
    await session.send(RunTasks())
    events: list[Event] = []
    async for event in session.events():
        events.append(event)
        if isinstance(event, TaskStarted):
            break

    await session.send(EditTask(task_id=one, task=TaskSpec(prompt="changed")))
    await session.send(AddTask(task=TaskSpec(prompt="two")))
    gate.set()
    events += await collect_run(session)

    assert ErrorOccurred(message=f"Task '{one}' can't be edited while it runs.") in events
    assert [p for _, p, _ in runner.log] == ["one", "two"]
    assert statuses(events) == ["completed", "completed"]


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
    assert await anext(stream) == VerificationChanged(steps=(StepState(name="always fails"),))


async def test_passing_verification_marks_the_task_completed(tmp_path: Path) -> None:
    (tmp_path / "ok").touch()
    session = AgentSession(
        RecordingRunner(), verification_steps=(file_exists_step(tmp_path / "ok"),)
    )
    [one] = await add_tasks(session, TaskSpec(prompt="one"))
    await session.send(RunTasks())
    events = await collect_run(session)

    def steps(status: str) -> VerificationChanged:
        return VerificationChanged(steps=(StepState(name="check", status=status),))  # type: ignore[arg-type]

    def task(status: str) -> TasksChanged:
        state = TaskState(id=one, task=TaskSpec(prompt="one"), status=status)  # type: ignore[arg-type]
        return TasksChanged(tasks=(state,))

    # Each task starts with a clean slate; once its turn ends it shows as verifying
    # while the step reports its progress.
    assert [e for e in events if not isinstance(e, TurnStarted | TurnFinished)][1:-1] == [
        task("running"),
        steps("pending"),
        TaskStarted(task_id=one),
        task("verifying"),
        steps("running"),
        VerificationStarted(task_id=one, step=0),
        steps("passed"),
        VerificationFinished(task_id=one, step=0, passed=True),
        task("completed"),
    ]


async def test_failed_verification_inserts_a_fix_task_after_the_task(tmp_path: Path) -> None:
    fixed = tmp_path / "fixed"
    runner = FixingRunner(fixed)
    session = AgentSession(runner, verification_steps=(file_exists_step(fixed),))
    settings = Settings(model="opus", tool_access="full")
    one, two = await add_tasks(
        session, TaskSpec(prompt="one", settings=settings), TaskSpec(prompt="two")
    )
    await session.send(RunTasks())
    events = await collect_run(session)

    assert VerificationFinished(task_id=one, step=0, passed=False) in events
    # One update marks the task unverified and adds its fix right behind it.
    first_done = next(
        e for e in events if isinstance(e, TasksChanged) and e.tasks[0].status == "unverified"
    )
    assert [(t.status, t.task.prompt.split("\n")[0]) for t in first_done.tasks] == [
        ("unverified", "one"),
        ("pending", "Fix the failed verification step: check"),
        ("pending", "two"),
    ]
    fix = first_done.tasks[1]
    # The fix runs with the failed task's settings, next, in its own context.
    assert fix.task.settings == settings
    assert [e.task_id for e in events if isinstance(e, TaskStarted)] == [one, fix.id, two]
    assert statuses(events) == ["unverified", "completed", "completed"]
    assert [p for _, p, _ in runner.log] == ["one", fix.task.prompt, "two"]
    assert len({id(r) for r, _, _ in runner.log}) == 3


async def test_verification_stops_at_the_first_failing_step(tmp_path: Path) -> None:
    session = AgentSession(
        RecordingRunner(),
        verification_steps=(FAILING_STEP, file_exists_step(tmp_path)),
        max_fix_attempts=0,
    )
    await add_tasks(session, TaskSpec(prompt="one"))
    await session.send(RunTasks())
    events = await collect_run(session)

    assert [e.step for e in events if isinstance(e, VerificationFinished)] == [0]
    assert step_statuses(events) == ["failed", "pending"]


async def test_fix_tasks_stop_after_max_attempts() -> None:
    runner = RecordingRunner()
    session = AgentSession(runner, verification_steps=(FAILING_STEP,), max_fix_attempts=2)
    await add_tasks(session, TaskSpec(prompt="one"), TaskSpec(prompt="two"))
    await session.send(RunTasks())
    events = await collect_run(session)

    # Each original task gets its own two attempts, then the run moves on.
    prompts = [p for _, p, _ in runner.log]
    fix_title = "Fix the failed verification step: always fails"
    assert [p.split("\n")[0] for p in prompts] == [
        "one",
        fix_title,
        fix_title,
        "two",
        fix_title,
        fix_title,
    ]
    assert statuses(events) == ["unverified"] * 6
    message = "Verification step 'always fails' still fails after 2 fix attempts."
    assert [e for e in events if isinstance(e, ErrorOccurred)] == [
        ErrorOccurred(message=message)
    ] * 2
    assert "nope" in prompts[1]


async def test_tasks_that_do_not_complete_are_not_verified() -> None:
    session = AgentSession(ModelLoopRunner(FailingModel()), verification_steps=(FAILING_STEP,))
    await add_tasks(session, TaskSpec(prompt="one"))
    await session.send(RunTasks())
    events = await collect_run(session)

    assert not any(isinstance(e, VerificationFinished) for e in events)
    assert statuses(events) == ["error"]
    assert step_statuses(events) == ["pending"]


async def test_cancel_during_verification_cancels_the_task_and_run() -> None:
    slow = VerificationStep(
        name="slow", command=(sys.executable, "-c", "import time; time.sleep(60)")
    )
    session = AgentSession(RecordingRunner(), verification_steps=(slow,))
    await add_tasks(session, TaskSpec(prompt="one"), TaskSpec(prompt="two"))
    await session.send(RunTasks())
    async for event in session.events():
        if isinstance(event, VerificationChanged) and event.steps[0].status == "running":
            break
    await session.send(CancelTurn())
    events = await asyncio.wait_for(collect_run(session), timeout=5)

    assert statuses(events) == ["cancelled", "pending"]
    assert step_statuses(events) == ["cancelled"]
    await asyncio.sleep(0.01)
    assert session._events.empty()


async def test_conversation_turns_are_not_verified() -> None:
    session = AgentSession(ModelLoopRunner(EchoModel()), verification_steps=(FAILING_STEP,))
    await session.send(SubmitPrompt(text="hello"))
    events = await collect_run(session)

    assert not any(isinstance(e, VerificationChanged | VerificationFinished) for e in events[1:])
    await asyncio.sleep(0.01)
    assert session._events.empty()
