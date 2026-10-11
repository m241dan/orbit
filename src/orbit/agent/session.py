"""In-process implementation of :class:`orbit.protocol.Session`."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Coroutine, Sequence
from typing import Any, Protocol, assert_never

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
    TaskSpec,
    TaskStarted,
    TurnEndReason,
    TurnFinished,
    UpdateSettings,
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
    """Runs turns one at a time in the background and publishes their events."""

    def __init__(
        self,
        runner: TurnRunner,
        settings: Settings | None = None,
        model_choices: Sequence[str] = (),
    ) -> None:
        self._runner = runner
        self._settings = settings or Settings()
        self._model_choices = tuple(model_choices)
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        self._events.put_nowait(self._settings_changed())
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
        for index, task in enumerate(tasks):
            await self._events.put(TaskStarted(index=index))
            runner = self._runner.new_context()
            reason = await self._run_turn(runner, task.prompt, task.settings)
            await self._events.put(TaskFinished(index=index, reason=reason))
            if reason == "cancelled":
                return

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
