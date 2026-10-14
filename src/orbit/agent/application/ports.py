"""Ports: what the application needs from the outside world. Adapters in
:mod:`orbit.agent.external` (or tests) implement them; the CLI picks which.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from orbit.agent.domain.agents import Agent
from orbit.agent.domain.conversation import Message, ModelResponse, ToolSpec
from orbit.agent.domain.verification import VerificationStep
from orbit.protocol import Event, Settings


class TurnRunner(Protocol):
    """Executes one turn, yielding ``TurnStarted`` first and ``TurnFinished`` last.

    Raising signals a failed turn; the session reports it. Cancellation arrives
    as ``asyncio.CancelledError`` and the runner must clean up and re-raise.
    """

    def run_turn(self, turn_id: str, prompt: str, settings: Settings) -> AsyncIterator[Event]: ...


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


@dataclass(frozen=True)
class Reply:
    """What orbit answers an agent's call with. ``is_error`` marks a refused or failed call."""

    text: str
    is_error: bool = False


CallHandler = Callable[[str, str, dict[str, Any]], Awaitable[Reply]]
"""Answers an agent's call: its token, the tool it called and the arguments."""


class AgentHost(Protocol):
    """Where agents reach orbit: a server that passes their tool calls to a handler."""

    async def start(self, handler: CallHandler) -> str:
        """Start serving calls to ``handler``; returns the URL agents connect to."""
        ...


class AgentLauncher(Protocol):
    """Starts agents as processes."""

    def run_agent(
        self, agent: Agent, url: str, prompt: str, settings: Settings
    ) -> AsyncIterator[Event]:
        """Start ``agent``, connected to orbit at ``url`` and told ``prompt``, and yield
        what it does until its process exits: ``TurnStarted`` first, ``TurnFinished`` last.

        Raising signals a crash. Cancellation arrives as ``asyncio.CancelledError``; the
        launcher must stop the process and re-raise.
        """
        ...
