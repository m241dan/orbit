"""Ports: what the application needs from the outside world. Adapters in
:mod:`orbit.agent.external` (or tests) implement them; the CLI picks which.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from orbit.agent.domain.agents import Agent
from orbit.agent.domain.roles import ChecksProposal, Setup
from orbit.agent.domain.usage import UsageFetch
from orbit.agent.domain.verification import VerificationStep
from orbit.protocol import Event, MergeResult, ProposalAction, Settings

Publish = Callable[[Event], Awaitable[None]]
"""Publishes an event to the frontend."""

StepRunner = Callable[[VerificationStep, str], Awaitable[tuple[bool, str]]]
"""Runs a verification step in a directory; returns whether it passed and its output."""

ConfigWriter = Callable[[str, ChecksProposal], None]
"""Makes accepted checks the verification steps and setup in the project config in a directory;
raises ``RuntimeError`` when it can't."""

UsageSource = Callable[[], Awaitable[UsageFetch]]
"""Fetches the subscription's usage limits once."""

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

    async def stop(self) -> None:
        """Stop serving."""
        ...


@dataclass(frozen=True)
class AgentLaunch:
    """How to start ``agent``: connected to orbit at ``url``, told ``prompt``, with ``settings``,
    working in the directory ``cwd``. ``instructions`` describe its role, and ``tools`` are the
    orbit tools it may call."""

    agent: Agent
    url: str
    prompt: str
    settings: Settings
    cwd: str
    instructions: str
    tools: tuple[str, ...]


class AgentLauncher(Protocol):
    """Starts agents as processes."""

    def run_agent(self, launch: AgentLaunch) -> AsyncIterator[Event]:
        """Start the agent as ``launch`` says, and yield what it does until its process
        exits: ``TurnStarted`` first, ``TurnFinished`` last.

        Raising signals a crash. Cancellation arrives as ``asyncio.CancelledError``; the
        launcher must stop the process and re-raise.
        """
        ...

    def converse(self, turn_id: str, launch: AgentLaunch) -> AsyncIterator[Event]:
        """Run one turn, ``turn_id``, of the agent's ongoing conversation, ``launch.prompt``
        being what it's told: the first turn starts it, and later ones carry it on. Yields and
        stops like :meth:`run_agent`."""
        ...


@dataclass(frozen=True)
class Merge:
    result: MergeResult
    detail: str = ""


class CoderTree(Protocol):
    """The working tree the coding agent works in, apart from the user's checkout, and the
    branch its commits are merged into from there.

    ``setup`` is what sets it up now; ``branch`` is the branch its commits are made on.
    """

    setup: Setup | None
    branch: str

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

    async def head(self) -> str:
        """The tree's latest commit."""
        ...

    async def base_commit(self) -> str:
        """The base's latest commit: the base branch's, or the main checkout's ``HEAD`` while
        there's no base branch. Works before the tree exists."""
        ...

    async def show(self, commits: Sequence[str]) -> str:
        """Each of ``commits``' message and diff."""
        ...

    async def snapshot(self, commit: str) -> str:
        """A separate, detached tree at ``commit`` to read, under a name of its own that no
        other snapshot, earlier or in another session, shares; returns its path."""
        ...

    async def drop(self, path: str) -> None:
        """Remove the snapshot at ``path``."""
        ...

    async def untracked(self) -> frozenset[str]:
        """Files in the tree git neither tracks nor ignores (a new directory as one entry)."""
        ...

    async def ignore(self, paths: Collection[str]) -> None:
        """Have git ignore ``paths`` from now on, without changing any tracked file."""
        ...

    async def leftovers(self) -> list[str]:
        """Notes on work left behind by earlier runs: unmerged commits, put-aside changes, and
        snapshots this tree didn't make (left in place, as another session may be using them)."""
        ...

    def use_setup(self, setup: Setup | None) -> None:
        """Set the tree up with ``setup`` from the next :meth:`prepare` on."""
        ...


@dataclass(frozen=True)
class ProposalDecision:
    """What the user made of a proposal: ``accept`` it, have the agent ``revise`` it as
    ``feedback`` says, or ``end`` the task."""

    action: ProposalAction
    feedback: str = ""


class ProposalReview(Protocol):
    """Where the user decides on the checks an agent proposed."""

    async def review(self, task_id: str, proposal: ChecksProposal) -> ProposalDecision:
        """Show the user ``proposal``, which task ``task_id``'s agent made and orbit tried;
        returns their decision."""
        ...
