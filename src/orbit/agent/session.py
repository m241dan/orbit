"""In-process implementation of :class:`orbit.protocol.Session`."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from typing import Any, Protocol, assert_never

from orbit.agent.board import TaskBoard
from orbit.agent.verify import MAX_FIX_ATTEMPTS, VerificationStep, fix_prompt, run_step
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
    SkipReason,
    SubmitPrompt,
    TasksChanged,
    TaskSpec,
    TaskStarted,
    TaskStatus,
    TurnEndReason,
    TurnFinished,
    UpdateSettings,
    VerificationChanged,
    VerificationFinished,
    VerificationSkipped,
    VerificationStarted,
)


class TurnRunner(Protocol):
    """Executes one turn, yielding ``TurnStarted`` first and ``TurnFinished`` last.

    Raising signals a failed turn; the session reports it. Cancellation arrives
    as ``asyncio.CancelledError`` and the runner must clean up and re-raise.
    """

    def run_turn(self, turn_id: str, prompt: str, settings: Settings) -> AsyncIterator[Event]: ...

    def new_context(self) -> "TurnRunner":
        """A runner with the same configuration and an empty conversation."""
        ...


# Per task: the fixes already queued for each step, by name, in the chain of fixes that
# led to it. Tasks the user queued have none.
Attempts = dict[str, dict[str, int]]


class AgentSession:
    """Runs turns and task runs one at a time in the background and publishes their events.

    The session owns the task list and reports it whole on every change. A run
    goes through it in order; after each task that completes,
    ``verification_steps`` run until one fails. A failure adds a task to fix it
    right after the failed one; a task's chain of fixes gets ``max_fix_attempts``
    for each step. Once a fix passes, the tasks it fixed count as completed.

    Tasks with tools off aren't verified, and neither are tasks that leave
    ``workspace`` (a fingerprint of the working tree) as it was. Without one,
    every task counts as changing files.
    """

    def __init__(
        self,
        runner: TurnRunner,
        settings: Settings | None = None,
        model_choices: Sequence[str] = (),
        verification_steps: Sequence[VerificationStep] = (),
        max_fix_attempts: int = MAX_FIX_ATTEMPTS,
        workspace: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        self._runner = runner
        self._settings = settings or Settings()
        self._model_choices = tuple(model_choices)
        self._steps = tuple(verification_steps)
        self._max_fix_attempts = max_fix_attempts
        self._workspace = workspace
        self._board = TaskBoard([s.name for s in self._steps])
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        self._events.put_nowait(self._settings_changed())
        if self._steps:
            self._events.put_nowait(VerificationChanged(steps=self._board.steps))
        self._turn: asyncio.Task[object] | None = None

    @property
    def runner(self) -> TurnRunner:
        return self._runner

    @property
    def workspace(self) -> Callable[[], Awaitable[object]] | None:
        return self._workspace

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def model_choices(self) -> tuple[str, ...]:
        return self._model_choices

    @property
    def verification_steps(self) -> tuple[VerificationStep, ...]:
        return self._steps

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
                if self._board.next_runnable() is not None:
                    await self._start(self._run_tasks())
            case _:
                assert_never(command)

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
        attempts: Attempts = {}
        while (task_id := self._board.next_runnable(skip=tried)) is not None:
            tried.add(task_id)
            if await self._run_task(task_id, attempts) == "cancelled":
                return

    async def _run_task(self, task_id: str, attempts: Attempts) -> TaskStatus:
        """Run one task in a new context, then verify it; returns its final status."""
        task = self._board.get(task_id).task
        # Before the task shows as running, so a cancel here leaves it pending.
        before = await self._state_before(task)
        self._board.set_status(task_id, "running")
        await self._publish_tasks()
        if self._steps:
            self._board.reset_steps()
            await self._publish_steps()
        await self._events.put(TaskStarted(task_id=task_id))
        status: TaskStatus = await self._run_turn(
            self._runner.new_context(), task.prompt, task.settings
        )
        if status == "completed" and self._steps:
            try:
                status = await self._check(task_id, task, before, attempts)
            except asyncio.CancelledError:
                status = "cancelled"
                self._board.cancel_running_steps()
                await self._publish_steps()
        self._board.set_status(task_id, status)
        if status == "completed":
            self._board.mark_fixed(task_id)
        await self._publish_tasks()
        return status

    async def _state_before(self, task: TaskSpec) -> object:
        """The working tree's state, when it will tell whether ``task`` changed files."""
        if self._workspace is None or not self._steps or task.settings.tool_access == "off":
            return None
        return await self._workspace()

    async def _check(
        self, task_id: str, task: TaskSpec, before: object, attempts: Attempts
    ) -> TaskStatus:
        """Verify a task whose turn completed, unless there's nothing to verify.

        Returns its status: ``completed``, or ``unverified`` after queueing a fix.
        """
        reason = await self._skip_reason(task, before)
        if reason is not None:
            self._board.skip_steps()
            await self._publish_steps()
            await self._events.put(VerificationSkipped(task_id=task_id, reason=reason))
            return "completed"
        self._board.set_status(task_id, "verifying")
        await self._publish_tasks()
        failure = await self._verify(task_id)
        if failure is None:
            return "completed"
        await self._queue_fix(task_id, attempts, *failure)
        return "unverified"

    async def _skip_reason(self, task: TaskSpec, before: object) -> SkipReason | None:
        if task.settings.tool_access == "off":
            return "tools_off"
        if before is None:
            return None  # Can't tell what changed, so verify.
        assert self._workspace is not None  # It produced ``before``.
        return "no_changes" if await self._workspace() == before else None

    async def _verify(self, task_id: str) -> tuple[VerificationStep, str] | None:
        """Run the steps after task ``task_id``; returns the first failure and its output."""
        for number, step in enumerate(self._steps):
            self._board.set_step(number, "running")
            await self._publish_steps()
            await self._events.put(VerificationStarted(task_id=task_id, step=number))
            passed, output = await run_step(step)
            self._board.set_step(number, "passed" if passed else "failed")
            await self._publish_steps()
            await self._events.put(
                VerificationFinished(task_id=task_id, step=number, passed=passed)
            )
            if not passed:
                return step, output
        return None

    async def _queue_fix(
        self, task_id: str, attempts: Attempts, step: VerificationStep, output: str
    ) -> None:
        """Add a task fixing ``step`` right after ``task_id``, unless its attempts ran out."""
        made = attempts.get(task_id, {})
        if made.get(step.name, 0) >= self._max_fix_attempts:
            message = (
                f"Verification step {step.name!r} still fails"
                f" after {self._max_fix_attempts} fix attempts."
            )
            await self._events.put(ErrorOccurred(message=message))
            return
        settings = self._board.get(task_id).task.settings
        fix = TaskSpec(prompt=fix_prompt(step, output), settings=settings)
        fix_id = self._board.add(fix, fixes=task_id)
        attempts[fix_id] = {**made, step.name: made.get(step.name, 0) + 1}

    async def _publish_tasks(self) -> None:
        await self._events.put(TasksChanged(tasks=self._board.tasks))

    async def _publish_steps(self) -> None:
        await self._events.put(VerificationChanged(steps=self._board.steps))

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
