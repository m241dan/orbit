"""Ports: what the application needs from the outside world. Adapters in
:mod:`orbit.agent.external` (or tests) implement them; the CLI picks which.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

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


StepRunner = Callable[[VerificationStep, str], Awaitable[tuple[bool, str]]]
"""Runs a verification step in a directory; returns whether it passed and its output."""

Workspace = Callable[[str], Awaitable[object]]
"""A fingerprint of the working tree in a directory, equal before and after a task that
changed no files; ``None`` when it can't be told."""


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


@dataclass(frozen=True)
class AgentLaunch:
    """How to start ``agent``: connected to orbit at ``url``, told ``prompt``, with ``settings``,
    working in the directory ``cwd``."""

    agent: Agent
    url: str
    prompt: str
    settings: Settings
    cwd: str


class AgentLauncher(Protocol):
    """Starts agents as processes."""

    def run_agent(self, launch: AgentLaunch) -> AsyncIterator[Event]:
        """Start the agent as ``launch`` says, and yield what it does until its process
        exits: ``TurnStarted`` first, ``TurnFinished`` last.

        Raising signals a crash. Cancellation arrives as ``asyncio.CancelledError``; the
        launcher must stop the process and re-raise.
        """
        ...


MergeResult = Literal["merged", "rebased", "conflict", "blocked", "detached"]
"""What came of merging the coder's commits into the base branch.

``merged``: the base now has them (or already did). ``rebased``: the base had moved, so they
were replayed on top of it and must be verified again before merging. ``conflict``: they
couldn't be replayed. ``blocked``: the base's checkout has changes in the way. ``detached``:
there's no base branch to merge into.
"""


@dataclass(frozen=True)
class Merge:
    result: MergeResult
    detail: str = ""


class CoderTree(Protocol):
    """The working tree the coding agent works in, apart from the user's checkout, and the
    branch its commits are merged into from there."""

    async def prepare(self, keep: bool) -> str:
        """Get the tree ready for a task and return its path. It catches up with the base
        branch; unless ``keep``, changes left in it are put aside (never deleted) first."""
        ...

    async def commit(self, message: str) -> bool:
        """Commit everything in the tree; returns whether there was anything to commit."""
        ...

    async def integrate(self) -> Merge:
        """Merge the tree's commits into the base branch, fast-forward only."""
        ...

    async def leftovers(self) -> list[str]:
        """Notes on work left behind by earlier runs: unmerged commits, put-aside changes."""
        ...
