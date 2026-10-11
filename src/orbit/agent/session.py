"""In-process implementation of :class:`orbit.protocol.Session`."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Coroutine, Sequence
from typing import Any, Protocol, assert_never

from orbit.agent.verify import MAX_FIX_ATTEMPTS, VerificationStep, fix_prompt, run_step
from orbit.protocol import (
    CancelTurn,
    Command,
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
    TurnEndReason,
    TurnFinished,
    UpdateSettings,
    VerificationConfigured,
    VerificationFinished,
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


class AgentSession:
    """Runs turns one at a time in the background and publishes their events.

    After each task that completes, ``verification_steps`` run in order until
    one fails. A failure queues a task to fix it right after the failed one,
    up to ``max_fix_attempts`` in a row.
    """

    def __init__(
        self,
        runner: TurnRunner,
        settings: Settings | None = None,
        model_choices: Sequence[str] = (),
        verification_steps: Sequence[VerificationStep] = (),
        max_fix_attempts: int = MAX_FIX_ATTEMPTS,
    ) -> None:
        self._runner = runner
        self._settings = settings or Settings()
        self._model_choices = tuple(model_choices)
        self._steps = tuple(verification_steps)
        self._max_fix_attempts = max_fix_attempts
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        self._events.put_nowait(self._settings_changed())
        if self._steps:
            self._events.put_nowait(
                VerificationConfigured(steps=tuple(s.name for s in self._steps))
            )
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
    def verification_steps(self) -> tuple[VerificationStep, ...]:
        return self._steps

    async def send(self, command: Command) -> None:
        match command:
            case SubmitPrompt(text=text):
                await self._start(self._run_turn(self._runner, text, self._settings))
            case RunTasks(tasks=tasks):
                await self._start(self._run_tasks(tasks))
            case CancelTurn():
                if self._turn is not None and not self._turn.done():
                    self._turn.cancel()
            case UpdateSettings(settings=settings):
                self._settings = settings
                await self._events.put(self._settings_changed())
            case _:
                assert_never(command)

    async def events(self) -> AsyncIterator[Event]:
        while True:
            yield await self._events.get()

    async def _start(self, work: Coroutine[Any, Any, object]) -> None:
        if self._turn is not None and not self._turn.done():
            work.close()
            await self._events.put(ErrorOccurred(message="A turn is already running."))
            return
        self._turn = asyncio.create_task(work)

    async def _run_tasks(self, tasks: Sequence[TaskSpec]) -> None:
        queue = list(tasks)
        # Fix tasks in a row before each task: 0 for the ones the batch started with.
        attempts = [0] * len(queue)
        index = 0
        while index < len(queue):
            task = queue[index]
            await self._events.put(TaskStarted(index=index))
            runner = self._runner.new_context()
            reason = await self._run_turn(runner, task.prompt, task.settings)
            verified: bool | None = None
            if reason == "completed" and self._steps:
                try:
                    failure = await self._verify(index)
                except asyncio.CancelledError:
                    reason = "cancelled"
                else:
                    verified = failure is None
                    if failure is not None:
                        await self._queue_fix(queue, attempts, index, *failure)
            await self._events.put(TaskFinished(index=index, reason=reason, verified=verified))
            if reason == "cancelled":
                return
            index += 1

    async def _verify(self, index: int) -> tuple[VerificationStep, str] | None:
        """Run the steps after task ``index``; returns the first failure and its output."""
        for number, step in enumerate(self._steps):
            await self._events.put(VerificationStarted(index=index, step=number))
            passed, output = await run_step(step)
            await self._events.put(VerificationFinished(index=index, step=number, passed=passed))
            if not passed:
                return step, output
        return None

    async def _queue_fix(
        self,
        queue: list[TaskSpec],
        attempts: list[int],
        index: int,
        step: VerificationStep,
        output: str,
    ) -> None:
        """Insert a task fixing ``step`` after task ``index``, unless attempts ran out."""
        if attempts[index] >= self._max_fix_attempts:
            message = (
                f"Verification step {step.name!r} still fails"
                f" after {self._max_fix_attempts} fix attempts."
            )
            await self._events.put(ErrorOccurred(message=message))
            return
        fix = TaskSpec(prompt=fix_prompt(step, output), settings=queue[index].settings)
        queue.insert(index + 1, fix)
        attempts.insert(index + 1, attempts[index] + 1)
        await self._events.put(TaskInserted(index=index + 1, task=fix))

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
