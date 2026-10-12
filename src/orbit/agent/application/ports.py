"""Ports: what the application needs from the outside world. Adapters in
:mod:`orbit.agent.external` (or tests) implement them; the CLI picks which.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any, Protocol

from orbit.agent.domain.conversation import Message, ModelResponse, ToolSpec
from orbit.agent.domain.verification import VerificationStep
from orbit.protocol import Event, Settings


class TurnRunner(Protocol):
    """Executes one turn, yielding ``TurnStarted`` first and ``TurnFinished`` last.

    Raising signals a failed turn; the session reports it. Cancellation arrives
    as ``asyncio.CancelledError`` and the runner must clean up and re-raise.
    """

    def run_turn(self, turn_id: str, prompt: str, settings: Settings) -> AsyncIterator[Event]: ...

    def new_context(self) -> "TurnRunner":
        """A runner with the same configuration and an empty conversation."""
        ...


class ModelClient(Protocol):
    async def complete(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> ModelResponse: ...


class Tool(Protocol):
    @property
    def spec(self) -> ToolSpec: ...

    async def run(self, arguments: dict[str, Any]) -> str: ...


StepRunner = Callable[[VerificationStep], Awaitable[tuple[bool, str]]]
"""Runs a verification step; returns whether it passed and its output."""

Workspace = Callable[[], Awaitable[object]]
"""A fingerprint of the working tree, equal before and after a task that changed no files;
``None`` when it can't be told."""
