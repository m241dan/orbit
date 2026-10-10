"""In-process implementation of :class:`orbit.protocol.Session`."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import assert_never

from orbit.agent.loop import DEFAULT_MAX_STEPS, run_turn
from orbit.agent.model import Message, ModelClient
from orbit.agent.tools import ToolRegistry
from orbit.protocol import (
    CancelTurn,
    Command,
    ErrorOccurred,
    Event,
    SubmitPrompt,
    TurnFinished,
)


class AgentSession:
    """Runs agent turns in the background and publishes their events."""

    def __init__(
        self,
        model: ModelClient,
        tools: ToolRegistry | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
    ) -> None:
        self._model = model
        self._tools = tools or ToolRegistry()
        self._max_steps = max_steps
        self._history: list[Message] = []
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        self._turn: asyncio.Task[None] | None = None

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
        checkpoint = len(self._history)
        self._history.append(Message(role="user", content=text))
        try:
            async for event in run_turn(
                turn_id, self._history, self._model, self._tools, self._max_steps
            ):
                await self._events.put(event)
        except asyncio.CancelledError:
            # Drop the partial turn so history never holds unanswered tool calls.
            del self._history[checkpoint:]
            await self._events.put(TurnFinished(turn_id=turn_id, reason="cancelled"))
        except Exception as exc:
            del self._history[checkpoint:]
            await self._events.put(ErrorOccurred(turn_id=turn_id, message=str(exc)))
            await self._events.put(TurnFinished(turn_id=turn_id, reason="error"))
