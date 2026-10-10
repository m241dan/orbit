"""In-process implementation of :class:`orbit.protocol.Session`."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Protocol, assert_never

from orbit.protocol import (
    CancelTurn,
    Command,
    ErrorOccurred,
    Event,
    SubmitPrompt,
    TurnFinished,
)


class TurnRunner(Protocol):
    """Executes one turn, yielding ``TurnStarted`` first and ``TurnFinished`` last.

    Raising signals a failed turn; the session reports it. Cancellation arrives
    as ``asyncio.CancelledError`` and the runner must clean up and re-raise.
    """

    def run_turn(self, turn_id: str, prompt: str) -> AsyncIterator[Event]: ...


class AgentSession:
    """Runs turns one at a time in the background and publishes their events."""

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        self._turn: asyncio.Task[None] | None = None

    @property
    def runner(self) -> TurnRunner:
        return self._runner

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
            case _:
                assert_never(command)

    async def events(self) -> AsyncIterator[Event]:
        while True:
            yield await self._events.get()

    async def _run(self, text: str) -> None:
        turn_id = uuid.uuid4().hex
        try:
            async for event in self._runner.run_turn(turn_id, text):
                await self._events.put(event)
        except asyncio.CancelledError:
            await self._events.put(TurnFinished(turn_id=turn_id, reason="cancelled"))
        except Exception as exc:
            await self._events.put(ErrorOccurred(turn_id=turn_id, message=str(exc)))
            await self._events.put(TurnFinished(turn_id=turn_id, reason="error"))
