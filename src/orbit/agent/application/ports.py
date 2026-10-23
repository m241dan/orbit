"""Ports: what the application needs from the outside world. Adapters in
:mod:`orbit.agent.external` (or tests) implement them; the CLI picks which.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from orbit.agent.domain.agents import Agent
from orbit.agent.domain.checks import ChecksProposal, Setup
from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.listings import FoundDirectory
from orbit.agent.domain.ownership import WorkspaceOwner
from orbit.agent.domain.replay import LogBounds
from orbit.agent.domain.roles import Role
from orbit.agent.domain.sessions import SessionRecord, Unreadable
from orbit.agent.domain.storage_halt import StorageErrorKind
from orbit.agent.domain.usage import UsageFetch
from orbit.agent.domain.usage_totals import UsageRow, UsageTotalsData
from orbit.agent.domain.verification import VerificationStep
from orbit.protocol import (
    Event,
    EventEnvelope,
    MergeResult,
    ProposalAction,
    Settings,
    WorkspaceCloning,
)

Publish = Callable[[Event], Awaitable[None]]
"""Publishes an event to the frontend."""

StepRunner = Callable[[VerificationStep, str], Awaitable[tuple[bool, str]]]
"""Runs a verification step in a directory; returns whether it passed and its output."""

ConfigWriter = Callable[[str, ChecksProposal], None]
"""Makes accepted checks the verification steps and setup in the project config in a directory;
raises :class:`ConfigWriteFailed` when it can't."""

PlannerRoleReader = Callable[[], Role | ConfigError]
"""Reads the planner's role from the project's config again: the role, or why the config is
invalid."""

UsageSource = Callable[[], Awaitable[UsageFetch]]
"""Fetches the subscription's usage limits once."""

Workspace = Callable[[str], Awaitable[object]]
"""A fingerprint of the working tree in a directory, equal before and after a task that
changed no files; ``None`` when it can't be told."""


class TreeFailed(Exception):
    """Git, or the coder's tree, failed, as the message says."""


class HostFailed(Exception):
    """A server orbit runs, such as the host agents reach it through, couldn't serve, as the
    message says."""


class ConfigWriteFailed(Exception):
    """Saving to the project's config file failed, as the message says."""


class CloneFailed(Exception):
    """Cloning a repository failed, as the message says: git's message, perhaps with a hint."""


class Cloner(Protocol):
    """What clones the repositories frontends ask a server to serve."""

    async def clone(self, url: str, progress: Callable[[WorkspaceCloning], None]) -> str:
        """The path of a new clone of the repository at ``url``, telling ``progress`` of git's
        progress as it clones. Raises :class:`CloneFailed` if it fails, having removed what it
        cloned; cancelled, it stops git and removes what it cloned before the cancellation
        ends."""
        ...


class DirectoryLister(Protocol):
    """What reads the directories on the server's host for frontends browsing them."""

    async def resolved(self, path: str) -> str:
        """``path``, absolute, with every symlink in it followed, as far as there's anything
        there."""
        ...

    async def directories(self, path: str) -> list[FoundDirectory]:
        """The directories in the directory at ``path``, symlinks to directories included.
        Raises :class:`OSError`, such as :class:`FileNotFoundError` or
        :class:`PermissionError`, if it can't be read."""
        ...


class RepositoryLock(Protocol):
    """The lock the orbit owning a repository holds."""

    def take(self) -> WorkspaceOwner | None:
        """Take the lock; returns its owner when another orbit holds it instead. Raises
        ``OSError`` when it can't be taken for another reason."""
        ...

    def close(self) -> None:
        """Let go of the lock, if this holds it."""
        ...


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
        """Start serving calls to ``handler``; returns the URL agents connect to. Raises
        :class:`HostFailed` when it can't serve."""
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


class ResumeFailed(RuntimeError):
    """A turn resuming a conversation failed before the CLI reported the conversation it ran
    in, as when that conversation is gone."""


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
        stops like :meth:`run_agent`; a turn carrying it on whose CLI exits before it's known
        to have done so raises :class:`ResumeFailed`."""
        ...

    def conversation(self, agent_id: str) -> str | None:
        """The id of the Claude conversation agent ``agent_id``'s turns carry on; ``None``
        before one has started it."""
        ...

    def set_conversation(self, agent_id: str, conversation_id: str | None) -> None:
        """Have agent ``agent_id``'s next turn carry on the conversation ``conversation_id``, or
        start a new one when it's ``None``."""
        ...


@dataclass(frozen=True)
class Merge:
    result: MergeResult
    detail: str = ""


class CoderTree(Protocol):
    """The working tree the coding agent works in, apart from the user's checkout, and the
    branch its commits are merged into from there.

    ``setup`` is what sets it up now; ``branch`` is the branch its commits are made on. Its
    methods raise :class:`TreeFailed` when git or the tree fails.
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

    async def holds_work(self) -> bool:
        """Whether the tree holds work: changes, or commits the base branch doesn't have."""
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


@dataclass(frozen=True)
class StoredSession:
    """A saved session: its session ``id``, its ``record`` or why that's unreadable, and whether
    another running orbit has it open (``locked``)."""

    id: str
    record: SessionRecord | Unreadable
    locked: bool


class StoreFailed(Exception):
    """A call to the store failed, as its message says."""


class ReadFailed(StoreFailed):
    """A read from the store failed, as its message says."""


class LockLeft(StoreFailed):
    """A session was deleted, but its lock file couldn't be removed, as the message says."""


class StorageFailed(StoreFailed):
    """A write to the store failed, as ``message`` says: ``database`` is its file, ``error`` the
    error as the database reported it, and ``kind`` what orbit makes of that."""

    def __init__(self, message: str, database: str, error: str, kind: StorageErrorKind) -> None:
        super().__init__(message)
        self.database = database
        self.error = error
        self.kind = kind


class Fallback(Protocol):
    """Where a session's events go when the store can't take them."""

    async def write(self, session_id: str, envelopes: Sequence[EventEnvelope]) -> str | None:
        """Add ``envelopes``, events of the session ``session_id``, to its fallback file, made
        owner-only in the first place that can be written; returns the file's path, or ``None``
        when no place could be written."""
        ...

    async def remove(self, session_id: str) -> None:
        """Delete the session ``session_id``'s fallback files, wherever they are; one that can't
        be deleted is left, to be imported again at the next start, its events skipped as
        stored."""
        ...


@dataclass(frozen=True)
class ParkedFile:
    """A fallback file an earlier run left: its ``path``, holding events of the session
    ``session_id``, and whether it's ``legacy``, named by its session alone as earlier orbits
    named it, so it may be another workspace's."""

    path: str
    session_id: str
    legacy: bool = False


class ParkedEvents(Protocol):
    """The fallback files of this workspace, in every place they go."""

    async def parked(self) -> list[ParkedFile]:
        """Every fallback file of this workspace, by path, and every legacy one that may be."""
        ...

    async def read(self, file: ParkedFile) -> list[EventEnvelope]:
        """The envelopes in ``file``, in the order written, but for those of types this orbit
        doesn't know; raises :class:`ReadFailed` when it can't be read or holds something else."""
        ...

    async def delete(self, file: ParkedFile) -> None:
        """Delete ``file``; raises :class:`StoreFailed` when it can't."""
        ...


class SeqMark(Protocol):
    """Where the highest ``seq`` handed out is marked, logged or not."""

    def max_seq(self) -> int:
        """The highest ``seq`` logged or reserved; 0 when none is."""
        ...

    async def reserve(self, seq: int) -> None:
        """Note that every ``seq`` up to ``seq`` may be handed out, logged or not, so that the
        highest ``seq`` logged counts it from now on and none of them is handed out again. Raises
        :class:`StoreFailed`, a :class:`StorageFailed` when the database failed, when it can't."""
        ...


class EventLog(SeqMark, Protocol):
    """Where sessions' events are kept, each session's in ``seq`` order."""

    async def latest_seq(self) -> int:
        """The highest ``seq`` logged or reserved now, by this orbit or another; 0 when none is.
        Raises :class:`ReadFailed` when it can't be read."""
        ...

    async def append(self, session_id: str, envelopes: Sequence[EventEnvelope]) -> int:
        """Add ``envelopes``, events of the session ``session_id``, to its log, all of them or
        none, skipping those whose ``seq`` it holds already; returns how many it added. Raises
        :class:`StoreFailed`, a :class:`StorageFailed` when the database failed, when it can't."""
        ...

    async def recent(self, session_id: str, limit: int) -> list[EventEnvelope]:
        """The last ``limit`` events logged of the session ``session_id``, oldest first. Raises
        :class:`ReadFailed` when they can't be read."""
        ...

    async def before(
        self, session_id: str, before_seq: int, limit: int
    ) -> tuple[list[EventEnvelope], bool]:
        """The last ``limit`` events logged of the session ``session_id`` before ``before_seq``,
        oldest first, and whether earlier ones remain. Raises :class:`ReadFailed` when they can't
        be read."""
        ...

    async def after(self, session_id: str, after_seq: int) -> list[EventEnvelope] | None:
        """The events logged of the session ``session_id`` after ``after_seq``, oldest first;
        ``None`` when they can't be read."""
        ...

    async def bounds(self, session_id: str, since: int) -> LogBounds | None:
        """Where the log of the session ``session_id`` stands in its workspace's, for a frontend
        that saw the events up to ``since``; ``None`` when that can't be read."""
        ...

    async def trim(self, session_id: str, limit: int, target: int) -> None:
        """When the log of the session ``session_id`` holds more than ``limit`` bytes, delete
        its oldest events until it holds less than ``target``, noting that it was trimmed;
        its newest event is always kept, so one of ``target`` bytes or more leaves the log over
        ``target``, or even ``limit``, until a later event is appended. Raises
        :class:`StorageFailed` when it can't."""
        ...

    async def trimmed(self, session_id: str) -> bool:
        """Whether events of the session ``session_id`` were trimmed from its log. Raises
        :class:`ReadFailed` when that can't be read."""
        ...

    async def forget(self, session_id: str) -> None:
        """Delete the events of the session ``session_id`` from its log, and its log with them;
        raises :class:`StorageFailed` when it can't."""
        ...


class SessionStore(EventLog, Protocol):
    """Where saved sessions are kept, each locked while an orbit has it open, with their events'
    log and the repository's count of commits since the last super review."""

    async def save(self, record: SessionRecord) -> None:
        """Save ``record``, replacing the one saved with its id; raises :class:`StoreFailed`, a
        :class:`StorageFailed` when the database failed, when it can't."""
        ...

    async def load(self, session_id: str) -> SessionRecord | Unreadable:
        """The record saved as ``session_id``, or why it can't be read (missing included)."""
        ...

    async def sessions(self) -> list[StoredSession]:
        """Every saved session, unreadable ones included, by id; none when none is saved. Raises
        :class:`ReadFailed` when the sessions can't be listed."""
        ...

    async def delete(self, session_id: str) -> None:
        """Delete the session ``session_id``, its events and its lock, readable or not; raises
        :class:`StoreFailed`, a :class:`StorageFailed` when the database failed, when it can't,
        or :class:`LockLeft` when the session was deleted but its lock file wasn't removed."""
        ...

    async def acquire(self, session_id: str) -> bool:
        """Lock the session ``session_id`` for this orbit; returns ``False`` when another
        running orbit has it locked. A lock left by one that ended is taken over. Raises
        :class:`StoreFailed` when the lock can't be taken."""
        ...

    async def release(self, session_id: str) -> None:
        """Unlock the session ``session_id``, if this orbit holds its lock; raises
        :class:`StoreFailed` when it can't."""
        ...

    async def review_count(self) -> int:
        """The commits since the last super review; 0 when none is saved. Raises
        :class:`ReadFailed` when the store can't be read."""
        ...

    async def save_review_count(self, count: int) -> None:
        """Save ``count`` as the commits since the last super review; raises
        :class:`StoreFailed`, a :class:`StorageFailed` when the database failed, when it can't."""
        ...


class UsageLedger(Protocol):
    """Where each finished turn's usage is recorded, across the workspaces, and added up."""

    async def record(self, row: UsageRow) -> None:
        """Record ``row``; raises :class:`StoreFailed`, a :class:`StorageFailed` when the
        database failed, when it can't."""
        ...

    async def totals(self, today_start: datetime) -> UsageTotalsData:
        """The totals per workspace and in all: today's, of the rows at or after
        ``today_start``, and lifetime, of every row plus what sessions spent before turns were
        recorded, as :func:`usage_totals` adds them up. Raises :class:`ReadFailed` when they
        can't be read."""
        ...
