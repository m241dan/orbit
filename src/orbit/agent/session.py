"""In-process implementation of :class:`orbit.protocol.Session`."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Protocol, assert_never

from orbit.protocol import (
    CancelTurn,
    Command,
    ErrorOccurred,
    Event,
    Settings,
    SettingsChanged,
    SubmitPrompt,
    TurnFinished,
    UpdateSettings,
)


class TurnRunner(Protocol):
    """Executes one turn, yielding ``TurnStarted`` first and ``TurnFinished`` last.

    Raising signals a failed turn; the session reports it. Cancellation arrives
    as ``asyncio.CancelledError`` and the runner must clean up and re-raise.
    """

    def run_turn(self, turn_id: str, prompt: str, settings: Settings) -> AsyncIterator[Event]: ...


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
        self._turn: asyncio.Task[None] | None = None

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
                if self._turn is not None and not self._turn.done():
                    await self._events.put(ErrorOccurred(message="A turn is already running."))
                    return
                self._turn = asyncio.create_task(self._run(text))
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

    async def _run(self, text: str) -> None:
        turn_id = uuid.uuid4().hex
        try:
            async for event in self._runner.run_turn(turn_id, text, self._settings):
                await self._events.put(event)
        except asyncio.CancelledError:
            await self._events.put(TurnFinished(turn_id=turn_id, reason="cancelled"))
        except Exception as exc:
            await self._events.put(ErrorOccurred(turn_id=turn_id, message=str(exc)))
            await self._events.put(TurnFinished(turn_id=turn_id, reason="error"))

    def _settings_changed(self) -> SettingsChanged:
        return SettingsChanged(settings=self._settings, model_choices=self._model_choices)
