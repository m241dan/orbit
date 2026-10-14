"""In-process implementation of :class:`orbit.protocol.Session`: the application's core.

It takes commands from the frontend, asks the domain what follows from them, drives the
ports to do it, and reports what happened as events.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Coroutine, Sequence
from typing import Any, assert_never

from orbit.agent.application.agent_run import AgentDesk, AgentRun, Agents
from orbit.agent.application.ports import TurnRunner
from orbit.agent.application.verifier import Verification, Verifier
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.verification import FixAttempts, VerificationStep, fix_prompt
from orbit.protocol import (
    AddTask,
    BusyChanged,
    CancelTurn,
    Command,
    EditTask,
    ErrorOccurred,
    Event,
    RunTasks,
    Settings,
    SettingsChanged,
    SubmitPrompt,
    TasksChanged,
    TaskSpec,
    TaskStarted,
    TaskStatus,
    TurnEndReason,
    TurnFinished,
    UpdateSettings,
    VerificationChanged,
)


def _verifier(
    verification: Verification | None, board: TaskBoard, events: "asyncio.Queue[Event]"
) -> Verifier | None:
    """What checks tasks on ``board``; none when ``verification`` has no steps."""
    if verification is None or not verification.steps:
        return None
    return Verifier(verification, board, events.put)


class AgentSession:
    """Runs turns and task runs one at a time in the background and publishes their events.

    The session owns the task list and reports it whole on every change. A run
    goes through it in order; after each task that completes, the ``verification``
    steps run until one fails. A failure adds a task to fix it right after the
    failed one. Once a fix passes, the tasks it fixed count as completed.

    Tasks with tools off aren't verified, and neither are tasks that leave the
    working tree as it was. A ``verification`` without steps counts as none.
    """

    def __init__(
        self,
        runner: TurnRunner,
        settings: Settings | None = None,
        model_choices: Sequence[str] = (),
        verification: Verification | None = None,
        agents: Agents | None = None,
    ) -> None:
        self._runner = runner
        self._settings = settings or Settings()
        self._model_choices = tuple(model_choices)
        steps = verification.steps if verification else ()
        self._board = TaskBoard([s.name for s in steps])
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        self._verifier = _verifier(verification, self._board, self._events)
        self._desk = AgentDesk(agents, self._events.put) if agents else None
        self._events.put_nowait(self._settings_changed())
        if self._verifier:
            self._events.put_nowait(VerificationChanged(steps=self._board.steps))
        self._turn: asyncio.Task[object] | None = None

    @property
    def runner(self) -> TurnRunner:
        return self._runner

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def model_choices(self) -> tuple[str, ...]:
        return self._model_choices

    @property
    def agents(self) -> Agents | None:
        return self._desk.agents if self._desk else None

    @property
    def verification(self) -> Verification | None:
        return self._verifier.verification if self._verifier else None

    @property
    def _checks(self) -> Verifier:
        assert self._verifier is not None  # Only used while verifying a task.
        return self._verifier

    async def send(self, command: Command) -> None:
        match command:
            case SubmitPrompt() | CancelTurn() | UpdateSettings():
                await self._handle_conversation(command)
            case AddTask() | EditTask() | RunTasks():
                await self._handle_tasks(command)
            case _:
                assert_never(command)

    async def _handle_conversation(
        self, command: SubmitPrompt | CancelTurn | UpdateSettings
    ) -> None:
        match command:
            case SubmitPrompt(text=text):
                await self._start(self._run_turn(self._runner, text, self._settings))
            case CancelTurn():
                self._cancel()
            case UpdateSettings(settings=settings):
                self._settings = settings
                await self._events.put(self._settings_changed())
            case _:
                assert_never(command)

    async def _handle_tasks(self, command: AddTask | EditTask | RunTasks) -> None:
        match command:
            case AddTask(task=task):
                self._board.add(task)
                await self._publish_tasks()
            case EditTask(task_id=task_id, task=task):
                await self._edit_task(task_id, task)
            case RunTasks():
                await self._run_tasks_if_possible()
            case _:
                assert_never(command)

    async def _run_tasks_if_possible(self) -> None:
        if self._desk is None:
            await self._events.put(ErrorOccurred(message="Tasks need agents, and none are set up."))
        elif self._board.next_runnable() is not None:
            await self._start(self._run_tasks())

    def _cancel(self) -> None:
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()

    async def _edit_task(self, task_id: str, task: TaskSpec) -> None:
        try:
            self._board.edit(task_id, task)
        except ValueError as exc:
            await self._events.put(ErrorOccurred(message=str(exc)))
        else:
            await self._publish_tasks()

    async def events(self) -> AsyncIterator[Event]:
        while True:
            yield await self._events.get()

    async def _start(self, work: Coroutine[Any, Any, object]) -> None:
        if self._turn is not None and not self._turn.done():
            work.close()
            await self._events.put(ErrorOccurred(message="A turn is already running."))
            return
        await self._events.put(BusyChanged(busy=True))
        self._turn = asyncio.create_task(work)
        # A callback, so it also runs if the work is cancelled before it starts.
        self._turn.add_done_callback(self._idle)

    def _idle(self, turn: "asyncio.Task[object]") -> None:
        # Work started since then reports its own end; staying quiet keeps it shown busy.
        if turn is self._turn:
            self._events.put_nowait(BusyChanged(busy=False))

    async def _run_tasks(self) -> None:
        tried: set[str] = set()
        attempts = FixAttempts()
        while (task_id := self._board.next_runnable(skip=tried)) is not None:
            tried.add(task_id)
            if await self._run_task(task_id, attempts) == "cancelled":
                return

    async def _run_task(self, task_id: str, attempts: FixAttempts) -> TaskStatus:
        """Run one task through a new agent; returns its final status."""
        task = self._board.get(task_id).task
        # Before the task shows as running, so a cancel here leaves it pending.
        before = await self._verifier.state_before(task, ".") if self._verifier else None
        self._board.set_status(task_id, "running")
        await self._publish_tasks()
        if self._verifier:
            self._board.reset_steps()
            await self._checks.publish_steps()
        await self._events.put(TaskStarted(task_id=task_id))
        assert self._desk is not None  # Runs only start with agents.
        status = await self._run_agent(self._desk, task_id, before, attempts)
        self._board.finish(task_id, status)
        await self._publish_tasks()
        return status

    async def _run_agent(
        self, desk: AgentDesk, task_id: str, before: object, attempts: FixAttempts
    ) -> TaskStatus:
        """Run task ``task_id`` through a new agent; a fallback queues a fix for a fresh one."""
        try:
            outcome = await AgentRun(task_id, self._board, self._verifier, desk).run(before, ".")
        except asyncio.CancelledError:
            if self._verifier:
                await self._verifier.cancel()
            return "cancelled"
        if outcome.failure is not None and outcome.status == "unverified":
            await self._queue_fix(task_id, attempts, *outcome.failure)
        return outcome.status

    async def _queue_fix(
        self, task_id: str, attempts: FixAttempts, step: VerificationStep, output: str
    ) -> None:
        """Add a task fixing ``step`` right after ``task_id``, unless its attempts ran out."""
        limit = self._checks.verification.max_fix_attempts
        if attempts.exhausted(task_id, step.name, limit):
            message = f"Verification step {step.name!r} still fails after {limit} fix attempts."
            await self._events.put(ErrorOccurred(message=message))
            return
        settings = self._board.get(task_id).task.settings
        fix = TaskSpec(prompt=fix_prompt(step, output), settings=settings)
        attempts.record(self._board.add(fix, fixes=task_id), task_id, step.name)

    async def _publish_tasks(self) -> None:
        await self._events.put(TasksChanged(tasks=self._board.tasks))

    async def _run_turn(self, runner: TurnRunner, text: str, settings: Settings) -> TurnEndReason:
        """Run one turn, publishing its events; returns how it ended."""
        turn_id = uuid.uuid4().hex
        reason: TurnEndReason = "completed"
        try:
            async for event in runner.run_turn(turn_id, text, settings):
                if isinstance(event, TurnFinished):
                    reason = event.reason
                await self._events.put(event)
        except asyncio.CancelledError:
            reason = "cancelled"
            await self._events.put(TurnFinished(turn_id=turn_id, reason=reason))
        except Exception as exc:
            reason = "error"
            await self._events.put(ErrorOccurred(turn_id=turn_id, message=str(exc)))
            await self._events.put(TurnFinished(turn_id=turn_id, reason=reason))
        return reason

    def _settings_changed(self) -> SettingsChanged:
        return SettingsChanged(settings=self._settings, model_choices=self._model_choices)
