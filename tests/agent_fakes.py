"""Fakes for running tasks through agents: a host and scripted agents stand in for MCP and the
CLI, and a step runner and workspace for verification."""

import asyncio
import itertools
import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from weakref import WeakKeyDictionary

from orbit.agent import Agents, ControlPlane, Verification, VerificationStep, WorkspaceHost
from orbit.agent.application.connection import SessionConnection
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.handshake import Handshake, HostGreeting
from orbit.agent.application.lanes import Background
from orbit.agent.application.planner import PlannerChat
from orbit.agent.application.ports import (
    AgentLaunch,
    CallHandler,
    Fallback,
    HostFailed,
    LockLeft,
    Merge,
    Publish,
    ReadFailed,
    Reply,
    RepositoryLock,
    SessionStore,
    StoredSession,
    StoreFailed,
    TreeFailed,
    UsageLedger,
    UsageSource,
)
from orbit.agent.application.review_count import ReviewCount
from orbit.agent.application.review_lane import ReviewLane
from orbit.agent.application.startup_check import StartupCheck
from orbit.agent.application.storage_halt import Retry, StorageHalt
from orbit.agent.application.switcher import SessionSwitcher
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.work_commands import WorkCommands
from orbit.agent.application.work_lane import WorkLane
from orbit.agent.application.work_turns import WorkTurns
from orbit.agent.application.workspace_host import UNREGISTERED_WORKSPACE
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.checks import Setup
from orbit.agent.domain.ownership import WorkspaceOwner
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.replay import LogBounds
from orbit.agent.domain.roles import Role, built_in_roles
from orbit.agent.domain.sessions import SessionRecord, Unreadable
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.agent.domain.usage import UsageFetch, UsageUnavailable
from orbit.agent.domain.usage_totals import UsageRow, UsageTotalsData, usage_totals
from orbit.agent.external.built_in_roles import read_built_in_roles
from orbit.protocol import (
    AGENT_FAILED,
    BUILT_IN_ROLE_NAMES,
    PLANNER,
    PROTOCOL_VERSION,
    REVIEW_ROLES,
    STORAGE_FAILED,
    AddTask,
    AgentExited,
    AgentReply,
    AssistantMessage,
    Attach,
    BusyChanged,
    BusyLane,
    Catalog,
    Command,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    ErrorOccurred,
    Event,
    EventEnvelope,
    Hello,
    History,
    HistoryPage,
    Notice,
    ResumeStorage,
    RunTasks,
    SendMessage,
    Session,
    SessionStateChanged,
    SessionSummary,
    Settings,
    SettingsChanged,
    Snapshot,
    TasksChanged,
    TaskSpec,
    TaskState,
    ToolCallStarted,
    TurnEndReason,
    TurnFinished,
    TurnStarted,
    Usage,
    UsageLimit,
    VerificationChanged,
    Welcome,
    encode,
)
from orbit.tracing import NoTelemetry, Telemetry
from tests.loop_quiet import quiet_within
from tests.waiting import eventually

Backend = WorkspaceHost | ControlPlane
"""A workspace host, driven through the control plane around it, or a control plane."""

ROLES = built_in_roles(
    read_built_in_roles(), {name: f"You are orbit's {name}." for name in BUILT_IN_ROLE_NAMES}
)
"""The built-in roles, with stand-in instructions."""

URL = "http://orbit.test/mcp"
Call = Callable[..., Awaitable[Reply]]
Script = Callable[[Call], Awaitable[TurnEndReason]]


class FakeHost:
    def __init__(self) -> None:
        self.handler: CallHandler | None = None
        self.starts = 0
        self.stops = 0

    async def start(self, handler: CallHandler) -> str:
        self.starts += 1
        self.handler = handler
        return URL

    async def stop(self) -> None:
        self.stops += 1

    async def call(self, token: str, tool: str, **arguments: Any) -> Reply:
        assert self.handler is not None
        return await self.handler(token, tool, arguments)


class GatedHost(FakeHost):
    """A host whose start waits for ``gate`` to be set; ``entered`` is set once it's waiting."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()

    async def start(self, handler: CallHandler) -> str:
        self.entered.set()
        await self.gate.wait()
        return await super().start(handler)


class FailingHost(FakeHost):
    """A host whose first ``failures`` starts fail, as uvicorn's might."""

    def __init__(self, failures: int = 1) -> None:
        super().__init__()
        self.failures = failures

    async def start(self, handler: CallHandler) -> str:
        if self.failures:
            self.failures -= 1
            raise HostFailed("Orbit's MCP server failed to start.")
        return await super().start(handler)


class ScriptedLauncher:
    """Each launch plays the next script as the agent, calling orbit through the host."""

    def __init__(self, host: FakeHost, *scripts: Script, reviewers: Sequence[Script] = ()) -> None:
        self.host = host
        self.scripts = list(scripts)
        self.reviewers = list(reviewers)
        self.launches: list[AgentLaunch] = []
        self.planner_turns: list[Script] = []  # Played by converse(), one per turn.
        self.planner_replies: list[str] = []  # What the planner says, a turn at a time.
        self.planner_usages: list[Usage] = []  # What planner turns cost; unmetered once out.
        # The error each planner turn ends in, one turn at a time, as the CLI reports it; none
        # once out.
        self.planner_errors: list[str] = []
        # What each planner turn does after its TurnFinished, a turn at a time.
        self.planner_afters: list[Callable[[], Awaitable[None]]] = []
        # The usage of each launch's turns, one launch at a time; one unmetered turn if none.
        self.usages: list[tuple[Usage, ...]] = []
        # The tools each launch's agent calls, by name, one launch at a time; none once out.
        self.tool_calls: list[tuple[str, ...]] = []
        # The error each launch's turn reports before it finishes, one launch at a time; none
        # once out, as the CLI reports one for a turn that ends in error.
        self.turn_errors: list[str] = []
        self.conversations: dict[str, str] = {}  # Agent id -> its conversation's id.
        self.resumed: list[str | None] = []  # The conversation each converse() carried on.

    async def run_agent(self, launch: AgentLaunch) -> AsyncIterator[Event]:
        self.launches.append(launch)
        agent = launch.agent
        reviewing = agent.role in REVIEW_ROLES
        script = (self.reviewers if reviewing else self.scripts).pop(0)
        for event in self._opening(agent.id):
            yield event

        async def call(tool: str, **arguments: Any) -> Reply:
            return await self.host.call(agent.token, tool, **arguments)

        reason = await script(call)
        if self.turn_errors:
            yield ErrorOccurred(
                turn_id=agent.id, code=AGENT_FAILED, message=self.turn_errors.pop(0)
            )
        for usage in self.usages.pop(0) if self.usages else (None,):
            yield TurnFinished(turn_id=agent.id, reason=reason, agent_id=agent.id, usage=usage)

    def _opening(self, agent_id: str) -> list[Event]:
        """What agent ``agent_id``'s turn starts with: its start, a message, and the next
        launch's tool calls, if any."""
        names = self.tool_calls.pop(0) if self.tool_calls else ()
        return [
            TurnStarted(turn_id=agent_id, agent_id=agent_id),
            AssistantMessage(turn_id=agent_id, text="on it", agent_id=agent_id),
            *(
                ToolCallStarted(
                    turn_id=agent_id,
                    agent_id=agent_id,
                    call_id=f"call-{n}",
                    name=name,
                    arguments={},
                )
                for n, name in enumerate(names)
            ),
        ]

    def conversation(self, agent_id: str) -> str | None:
        return self.conversations.get(agent_id)

    def set_conversation(self, agent_id: str, conversation_id: str | None) -> None:
        if conversation_id is None:
            self.conversations.pop(agent_id, None)
        else:
            self.conversations[agent_id] = conversation_id

    async def converse(self, turn_id: str, launch: AgentLaunch) -> AsyncIterator[Event]:
        """A turn of the next planner script, which starts the conversation
        ``conversation-<agent id>`` unless it carries one on."""
        self.launches.append(launch)
        agent = launch.agent
        self.resumed.append(self.conversations.get(agent.id))
        yield TurnStarted(turn_id=turn_id, agent_id=agent.id)
        if self.planner_replies:
            text = self.planner_replies.pop(0)
            yield AssistantMessage(turn_id=turn_id, text=text, agent_id=agent.id)

        async def call(tool: str, **arguments: Any) -> Reply:
            return await self.host.call(agent.token, tool, **arguments)

        reason = await self.planner_turns.pop(0)(call)
        self.conversations.setdefault(agent.id, f"conversation-{agent.id}")
        usage = self.planner_usages.pop(0) if self.planner_usages else None
        if self.planner_errors:
            message = self.planner_errors.pop(0)
            yield ErrorOccurred(
                turn_id=turn_id, agent_id=agent.id, code=AGENT_FAILED, message=message
            )
            reason = "error"
        yield TurnFinished(turn_id=turn_id, reason=reason, agent_id=agent.id, usage=usage)
        if self.planner_afters:
            await self.planner_afters.pop(0)()


class Steps:
    """A step runner whose step passes or fails by a script of results, recording runs."""

    def __init__(self, *results: bool) -> None:
        self.results = list(results)
        self.runs: list[str] = []
        self.roots: list[str] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, step: VerificationStep, root: str) -> tuple[bool, str]:
        self.runs.append(step.name)
        self.roots.append(root)
        if self.gate is not None:
            await self.gate.wait()
        passed = self.results.pop(0)
        return passed, "all good" if passed else "1 test failed"


START = datetime(2026, 10, 17, 14, 30, tzinfo=UTC)
"""When a :class:`Ticks` clock starts."""


class Ticks:
    """A clock that starts at :data:`START` and moves a minute on each time it's read."""

    def __init__(self) -> None:
        self.reads = 0

    def __call__(self) -> datetime:
        self.reads += 1
        return START + timedelta(minutes=self.reads - 1)


HALTED = "The session is halted."
"""Why :class:`FakeHalt` refuses a command while halted."""


class FakeHalt:
    """Records the store's failures it's told of, each as the write and the error's text; halted
    from the first, refusing commands with :data:`HALTED`."""

    def __init__(self) -> None:
        self.failures: list[tuple[FailedWrite, str]] = []

    @property
    def halted(self) -> bool:
        return bool(self.failures)

    def fail(self, write: FailedWrite, error: StoreFailed) -> None:
        self.failures.append((write, str(error)))

    def check(self) -> None:
        if self.failures:
            raise Refused(STORAGE_FAILED, HALTED)


class FakeLedger:
    """A usage ledger in memory: the ``rows`` recorded, oldest first; while ``failure`` is set,
    recording fails with it."""

    def __init__(self) -> None:
        self.rows: list[UsageRow] = []
        self.failure: str | None = None

    async def record(self, row: UsageRow) -> None:
        if self.failure is not None:
            raise StoreFailed(self.failure)
        self.rows.append(row)

    async def totals(self, today_start: datetime) -> UsageTotalsData:
        return usage_totals({}, {}, {})


class FakeFallback:
    """Fallback files kept in memory: each session's envelopes in ``files``, its file named by
    :meth:`path`; while ``broken``, nothing can be written. A file removed is gone from
    ``files``."""

    def __init__(self) -> None:
        self.files: dict[str, list[EventEnvelope]] = {}
        self.broken = False

    @staticmethod
    def path(session_id: str) -> str:
        return f"/state/fallback/w1-{session_id}.jsonl"

    async def write(self, session_id: str, envelopes: Sequence[EventEnvelope]) -> str | None:
        if self.broken:
            return None
        await asyncio.sleep(0)  # Writing takes a while.
        self.files.setdefault(session_id, []).extend(envelopes)
        return self.path(session_id)

    async def remove(self, session_id: str) -> None:
        self.files.pop(session_id, None)


class FakeStore:
    """Saved sessions kept in memory, holding the super review ``count``.

    ``failure``, while set, is the error every session save raises, ``count_failure`` the error
    every count save raises, and ``count_load_failure`` the error reading the count raises;
    ``count_yields`` lists how many times each count save in turn yields before it's written
    (none once it runs out). ``free`` says whether the lock can be taken, and ``lock_failure``,
    if set, is the error taking it raises. Sessions in ``elsewhere`` are locked by another
    orbit, those in ``broken`` are unreadable for the reason given, and ``delete_failure``, if
    set, is the error deleting one raises, ``lock_left`` the error raised once one is deleted but
    its lock isn't, and ``release_failure`` the error letting go of a
    lock raises, and ``load_failure``, if set, the reason every session loaded is unreadable,
    as a database error makes it, and ``list_failure`` the error listing them raises.
    ``on_acquire``, if set, is awaited as a lock is taken, ``on_release`` as a lock is let go
    of, ``on_delete`` as a session is deleted, ``on_list`` once the sessions are listed but
    before they're returned, and ``on_recent`` with a session's id as its recent events are
    read.

    The events appended are ``logged``, each append's in ``appends``, with ``seq`` the highest
    logged before or deleted since; ``append_failure``, while set, is the error every append
    raises. Each trim asked for is in ``trims``, as the session's id, the limit and the target,
    but no event is trimmed: tests of trimming itself use the real store. The sessions in
    ``trimmed_logs`` count as trimmed. The log's size is that of the events logged, as encoded.
    Each session whose events were forgotten is in ``forgotten``, and ``forgotten_seq`` is the
    highest ``seq`` forgotten or deleted with a session, counted as another session's in the
    bounds; ``forget_failure``, while set, is the error forgetting raises. Each ``seq`` reserved
    is in ``reservations``, counted in the mark as the port says; ``reserve_failure``, while
    set, is the error reserving raises.
    ``after_failure`` and ``bounds_failure``, while set, are the errors reading the events after
    a ``seq`` and the bounds raise, as :class:`ReadFailed`.

    The record moves kept are ``moves``; ``moves_failure``, while set, is the error saving them
    raises, and ``moves_load_failure`` the error reading them raises, as :class:`ReadFailed`.
    """

    def __init__(self, count: int = 0) -> None:
        self.saved: list[SessionRecord] = []
        self.locked: set[str] = set()
        self.released: list[str] = []
        self.count = count
        self.counts: list[int] = []  # Each count saved.
        self.failure: str | None = None
        self.count_failure: str | None = None
        self.count_load_failure: str | None = None
        self.count_yields: list[int] = []
        self.free = True
        self.lock_failure: str | None = None
        self.elsewhere: set[str] = set()
        self.broken: dict[str, str] = {}
        self.deleted: list[str] = []
        self.delete_failure: str | None = None
        self.lock_left: str | None = None
        self.release_failure: str | None = None
        self.load_failure: str | None = None
        self.list_failure: BaseException | None = None
        self.on_acquire: Callable[[], Awaitable[None]] | None = None
        self.on_release: Callable[[], Awaitable[None]] | None = None
        self.on_delete: Callable[[], Awaitable[None]] | None = None
        self.on_list: Callable[[], Awaitable[None]] | None = None
        self.on_recent: Callable[[str], Awaitable[None]] | None = None
        self.seq = 0
        self.logged: list[EventEnvelope] = []
        self.appends: list[tuple[str, list[EventEnvelope]]] = []
        self.append_failure: str | None = None
        self.trims: list[tuple[str, int, int]] = []
        self.trimmed_logs: set[str] = set()
        self.forgotten: list[str] = []
        self.forgotten_seq = 0
        self.forget_failure: str | None = None
        self.reservations: list[int] = []
        self.reserve_failure: str | None = None
        self.after_failure: str | None = None
        self.bounds_failure: str | None = None
        self.moves: dict[str, str] = {}
        self.moves_failure: str | None = None
        self.moves_load_failure: str | None = None

    @property
    def last(self) -> SessionRecord:
        return self.saved[-1]

    async def save(self, record: SessionRecord) -> None:
        if self.failure is not None:
            raise StoreFailed(self.failure)
        await asyncio.sleep(0)  # Saving takes a while, as writing a file does.
        self.saved.append(record)

    async def load(self, session_id: str) -> SessionRecord | Unreadable:
        if self.load_failure is not None:
            return Unreadable(self.load_failure)
        if session_id in self.broken:
            return Unreadable(self.broken[session_id])
        found = [r for r in self.saved if r.id == session_id]
        return found[-1] if found else Unreadable("it doesn't exist")

    async def sessions(self) -> list[StoredSession]:
        if self.list_failure is not None:
            raise self.list_failure
        latest: dict[str, SessionRecord | Unreadable] = {r.id: r for r in self.saved}
        latest |= {i: Unreadable(reason) for i, reason in self.broken.items()}
        listed = [StoredSession(i, r, i in self.elsewhere) for i, r in sorted(latest.items())]
        if self.on_list is not None:
            await self.on_list()
        return listed

    async def delete(self, session_id: str) -> None:
        if self.on_delete is not None:
            await self.on_delete()
        if self.delete_failure is not None:
            raise StoreFailed(self.delete_failure)
        self.deleted.append(session_id)
        self.saved = [r for r in self.saved if r.id != session_id]
        self._forget_log(session_id)
        if self.lock_left is not None:
            raise LockLeft(self.lock_left)
        self.locked.discard(session_id)

    async def acquire(self, session_id: str) -> bool:
        if self.on_acquire is not None:
            await self.on_acquire()
        if self.lock_failure is not None:
            raise StoreFailed(self.lock_failure)
        if session_id in self.elsewhere:
            return False
        if self.free:
            self.locked.add(session_id)
        return self.free

    async def release(self, session_id: str) -> None:
        if self.on_release is not None:
            await self.on_release()
        if self.release_failure is not None:
            raise StoreFailed(self.release_failure)
        self.released.append(session_id)
        self.locked.discard(session_id)

    def max_seq(self) -> int:
        return max([self.seq, *(envelope.seq for envelope in self.logged), *self.reservations])

    async def latest_seq(self) -> int:
        await asyncio.sleep(0)  # Reading takes a while.
        return self.max_seq()

    async def reserve(self, seq: int) -> None:
        if self.reserve_failure is not None:
            raise StoreFailed(self.reserve_failure)
        await asyncio.sleep(0)  # Writing takes a while.
        self.reservations.append(seq)

    async def append(self, session_id: str, envelopes: Sequence[EventEnvelope]) -> int:
        if self.append_failure is not None:
            raise StoreFailed(self.append_failure)
        await asyncio.sleep(0)  # Writing takes a while.
        self.appends.append((session_id, list(envelopes)))
        stored = {envelope.seq for envelope in self.logged}
        added = [envelope for envelope in envelopes if envelope.seq not in stored]
        self.logged += added
        return len(added)

    async def recent(self, session_id: str, limit: int) -> list[EventEnvelope]:
        if self.on_recent is not None:
            await self.on_recent(session_id)
        await asyncio.sleep(0)  # Reading takes a while.
        return [envelope for envelope in self.logged if envelope.session == session_id][-limit:]

    async def before(
        self, session_id: str, before_seq: int, limit: int
    ) -> tuple[list[EventEnvelope], bool]:
        await asyncio.sleep(0)  # Reading takes a while.
        logged = self.logged
        earlier = [e for e in logged if e.session == session_id and e.seq < before_seq]
        return earlier[-limit:], len(earlier) > limit

    async def after(self, session_id: str, after_seq: int) -> list[EventEnvelope]:
        await self._read(self.after_failure)
        return [e for e in self.logged if e.session == session_id and e.seq > after_seq]

    async def bounds(self, session_id: str, since: int) -> LogBounds:
        await self._read(self.bounds_failure)
        own = [e.seq for e in self.logged if e.session == session_id]
        others = {e.seq for e in self.logged} - set(own)
        return LogBounds(
            first_seq=min(own, default=None),
            others_seq=max([self.forgotten_seq, *others]),
            own_seq=max((seq for seq in own if seq <= since), default=None),
        )

    async def _read(self, failure: str | None) -> None:
        """Take a while to read, as a database does, or raise ``failure`` if it's set."""
        if failure is not None:
            raise ReadFailed(failure)
        await asyncio.sleep(0)

    async def trim(self, session_id: str, limit: int, target: int) -> None:
        self.trims.append((session_id, limit, target))

    async def trimmed(self, session_id: str) -> bool:
        return session_id in self.trimmed_logs

    def log_size(self) -> int:
        return sum(len(encode(envelope).encode()) for envelope in self.logged)

    async def forget(self, session_id: str) -> None:
        if self.forget_failure is not None:
            raise StoreFailed(self.forget_failure)
        self.forgotten.append(session_id)
        self._forget_log(session_id)

    def _forget_log(self, session_id: str) -> None:
        """Drop the session ``session_id``'s events, raising ``forgotten_seq`` and ``seq`` to the
        highest dropped, so neither the bounds nor the mark lose it."""
        dropped = [envelope.seq for envelope in self.logged if envelope.session == session_id]
        self.forgotten_seq = max([self.forgotten_seq, *dropped])
        self.seq = max([self.seq, *dropped])
        self.logged = [envelope for envelope in self.logged if envelope.session != session_id]

    async def review_count(self) -> int:
        if self.count_load_failure is not None:
            raise StoreFailed(self.count_load_failure)
        return self.count

    async def save_review_count(self, count: int) -> None:
        if self.count_failure is not None:
            raise StoreFailed(self.count_failure)
        for _ in range(self.count_yields.pop(0) if self.count_yields else 0):
            await asyncio.sleep(0)  # Saving takes a while, as writing a file does.
        self.count = count
        self.counts.append(count)

    async def record_moves(self) -> dict[str, str]:
        await self._read(self.moves_load_failure)
        return dict(self.moves)

    async def save_record_moves(self, moves: Mapping[str, str]) -> None:
        if self.moves_failure is not None:
            raise StoreFailed(self.moves_failure)
        await asyncio.sleep(0)  # Saving takes a while, as writing a file does.
        self.moves |= moves


STEP = VerificationStep(name="Unit tests", command=("pytest",))


def python_step(code: str, name: str = "check") -> VerificationStep:
    """A step named ``name`` that runs Python ``code``."""
    return VerificationStep(name=name, command=(sys.executable, "-c", code))


class FakeWorkspace:
    """Reports ``states`` in turn as the working tree's state."""

    def __init__(self, *states: object) -> None:
        self.states = list(states)

    async def __call__(self, root: str) -> object:
        return self.states.pop(0)


class FakeTree:
    """A coder tree that records what it's asked to do and merges as scripted."""

    path = "/work/coder"
    branch = "orbit/coder"
    base = "base"  # The base branch's commit, apart from the tree's own heads.

    def __init__(
        self, *merges: Merge, leftovers: tuple[str, ...] = (), broken: str = "", empty: bool = False
    ) -> None:
        self.empty = empty  # Tasks change nothing, so there's nothing to commit.
        self.agent_commits = 0  # Commits agents made themselves.
        self.untracked_sets: list[frozenset[str]] = []  # Scripted answers to untracked().
        self.ignored: list[set[str]] = []
        self.merges = list(merges)
        self.notes = list(leftovers)
        self.broken = broken
        self.prepared: list[bool] = []
        self.holding = True  # What holds_work() says: whether the tree holds work.
        self.commits: list[str] = []
        self.snapshots: list[str] = []
        self.dropped: list[str] = []
        self.setups: list[Setup | None] = []  # Each setup it was told to use, in order.
        self.setup: Setup | None = None
        self.failures: dict[str, Exception] = {}  # Method name -> what its next call raises.

    async def prepare(self, keep: bool) -> str:
        self.prepared.append(keep)
        self._fail("prepare")
        if self.broken:
            raise TreeFailed(self.broken)
        return self.path

    async def commit(self, message: str) -> bool:
        if self.empty:
            return False
        self.commits.append(message)
        return True

    async def integrate(self) -> Merge:
        return self.merges.pop(0) if self.merges else Merge("merged")

    async def holds_work(self) -> bool:
        return self.holding

    async def head(self) -> str:
        if not self.prepared:  # Like git in a worktree that doesn't exist yet.
            raise TreeFailed("git rev-parse failed: the coder's tree doesn't exist")
        return f"c{len(self.commits) + self.agent_commits}"

    async def base_commit(self) -> str:
        self._fail("base_commit")
        return self.base

    async def show(self, commits: Sequence[str]) -> str:
        self._fail("show")
        return "\n".join(f"diff of {commit}" for commit in commits)

    async def snapshot(self, commit: str) -> str:
        self._fail("snapshot")
        self.snapshots.append(commit)
        return f"/work/review-{commit}"

    async def drop(self, path: str) -> None:
        self.dropped.append(path)
        self._fail("drop")

    async def untracked(self) -> frozenset[str]:
        return self.untracked_sets.pop(0) if self.untracked_sets else frozenset()

    async def ignore(self, paths: Collection[str]) -> None:
        self.ignored.append(set(paths))

    async def leftovers(self) -> list[str]:
        self._fail("leftovers")
        return self.notes

    def use_setup(self, setup: Setup | None) -> None:
        self.setups.append(setup)
        self.setup = setup

    def _fail(self, method: str) -> None:
        """Raise the failure scripted for ``method``, if any; it fails once."""
        failure = self.failures.pop(method, None)
        if failure is not None:
            raise failure


def verified(
    steps: Steps, workspace: FakeWorkspace | None = None, fallback_fixes: int = 3
) -> Verification:
    return Verification((STEP,), steps, workspace, fallback_fixes)


LOCK_RETRY = 0.01
"""How often a test's read-only session tries the repository's lock, in seconds."""

LOCK_DECLINED_RETRY = 60.0
"""How long a test's read-only session holds off offering a repository it was declined: longer
than any test runs."""


async def nothing_parked() -> Sequence[str]:
    """No notes, as no fallback files are parked."""
    return ()


def agents_for(
    *scripts: Script,
    reviewers: Sequence[Script] = (),
    host: FakeHost | None = None,
    tokens: str = "token",
    **options: Any,
) -> tuple[Agents, ScriptedLauncher]:
    """Agents whose tasks run through agents playing ``scripts`` and whose reviews run through
    agents playing ``reviewers``, one per launch, with the launcher that plays them. They reach
    orbit through ``host``, or a new :class:`FakeHost`, with the tokens ``<tokens>-1``,
    ``<tokens>-2`` and so on.

    ``options`` go to :class:`Agents`, such as ``limits``, ``tree`` or ``roles`` (:data:`ROLES`
    unless given); agents work in ``root``, ``.`` unless given, and have ``exit_grace``, half a
    second unless given, to exit.
    """
    host = host or FakeHost()
    launcher = ScriptedLauncher(host, *scripts, reviewers=reviewers)
    numbers = itertools.count(1)
    options = {"limits": Limits(), "exit_grace": 0.5, "roles": ROLES, "root": ".", **options}
    return Agents(host, launcher, lambda: f"{tokens}-{next(numbers)}", **options), launcher


def desk_for(publish: Publish, *scripts: Script, **options: Any) -> AgentDesk:
    """A desk publishing with ``publish``, whose agents play ``scripts``, one per launch;
    ``options`` go to :func:`agents_for`."""
    agents, _ = agents_for(*scripts, **options)
    return AgentDesk(agents, publish)


def session_for(
    *scripts: Script,
    verification: Verification | None = None,
    reviewers: Sequence[Script] = (),
    check_at_startup: bool = False,
    usage: UsageSource | None = None,
    store: SessionStore | None = None,
    fallback: Fallback | None = None,
    recover: Callable[[], Awaitable[Sequence[str]]] | None = None,
    owner: WorkspaceOwner | None = None,
    lock: RepositoryLock | None = None,
    lock_declined_retry: float = LOCK_DECLINED_RETRY,
    ledger: UsageLedger | None = None,
    telemetry: Telemetry | None = None,
    **options: Any,
) -> tuple[WorkspaceHost, FakeHost, ScriptedLauncher]:
    """A session whose tasks run through agents playing ``scripts``, one per launch, and
    whose reviews run through agents playing ``reviewers``, with usage from ``usage``. It's
    saved in ``store``, if given, as ``session-1``, on a :class:`Ticks` clock, its unsaved
    events going to ``fallback``, if given, recovering parked ones with ``recover``, if given,
    and read-only if another ``owner`` owns the
    repository, trying its ``lock``, if given, every :data:`LOCK_RETRY` seconds and offering it
    again ``lock_declined_retry`` seconds after a decline. Its turns' usage is recorded in
    ``ledger``, if given, and its work traced with ``telemetry``, if given.

    ``options`` go to :func:`agents_for`, and so to :class:`Agents`, such as ``limits``,
    ``tree`` or ``roles`` (:data:`ROLES` unless given).
    """
    agents, launcher = agents_for(*scripts, reviewers=reviewers, **options)
    session = WorkspaceHost(
        verification=verification,
        agents=agents,
        check_at_startup=check_at_startup,
        store=store,
        clock=Ticks(),
        new_id=lambda: "session-1",
        fallback=fallback,
        recover=recover or nothing_parked,
        owner=owner,
        lock=lock,
        lock_retry=LOCK_RETRY,
        lock_declined_retry=lock_declined_retry,
        ledger=ledger,
        telemetry=telemetry or NoTelemetry(),
    )
    if usage is not None:
        served(session, usage)
    return session, launcher.host, launcher


COMMAND_ID = "command"
"""The id :func:`send` sends every command under."""

ACCEPTED = CommandAccepted(command_id=COMMAND_ID)
"""The outcome of a command :func:`send` sent that was accepted."""


def refused(code: str, message: str) -> CommandRefused:
    """The outcome of a command :func:`send` sent that was refused, for ``code``, with
    ``message``."""
    return CommandRefused(command_id=COMMAND_ID, code=code, message=message)


HELLO_ID = "hello"
"""The id :func:`greet` sends Hello under."""

HELLO = Hello(client="orbit-tests", protocol_version=PROTOCOL_VERSION)
"""The Hello :func:`greet` sends."""

WORKSPACE = UNREGISTERED_WORKSPACE
"""The id of the workspace a test's host serves, as it's given none."""

ATTACH_ID = "attach"
"""The id :func:`greet` sends Attach under."""


def host_of(session: Backend) -> WorkspaceHost:
    """``session``, if it's a workspace host, else the one the control plane serves."""
    if isinstance(session, ControlPlane):
        [host] = session.hosts
        return host
    return session


def attach_to(session: Backend, session_id: str | None = None) -> Attach:
    """The Attach to the session ``session_id`` of ``session``'s workspace, or to its open one
    if that's none."""
    host = host_of(session)
    return Attach(workspace=host.workspace_id, session=session_id or host.saved_session.id)


async def greet(session: Session | Backend) -> None:
    """Open ``session``'s stream with Hello, then, for a :data:`Backend`, attach to its open
    session, as a frontend does before anything else."""
    await frontend(session).send(CommandEnvelope(command_id=HELLO_ID, payload=HELLO))
    if isinstance(session, WorkspaceHost | ControlPlane):
        attach = CommandEnvelope(command_id=ATTACH_ID, payload=attach_to(session))
        await frontend(session).send(attach)


async def send(session: Session | Backend, command: Command) -> None:
    """Send ``command`` to ``session``, in an envelope for the open session, greeting a
    :data:`Backend` first if it hasn't been."""
    await greeted(session)
    await frontend(session).send(CommandEnvelope(command_id=COMMAND_ID, payload=command))


async def payloads(session: Session | Backend) -> AsyncIterator[Event]:
    """The events in ``session``'s envelopes but for its ``Welcome``, its catalogs, its
    sessions' state changes and the acceptance of the Attach :func:`greet` sends, greeting a
    :data:`Backend` first if it hasn't been."""
    await greeted(session)
    async for envelope in frontend(session).events():
        if not greeting_only(envelope.payload):
            yield envelope.payload


def greeting_only(event: Event) -> bool:
    """Whether :func:`payloads` leaves ``event`` out."""
    attached = isinstance(event, CommandAccepted) and event.command_id == ATTACH_ID
    return attached or isinstance(event, Welcome | Catalog | SessionStateChanged)


async def run(session: WorkspaceHost, *tasks: TaskSpec) -> list[Event]:
    """Queue ``tasks``, run them, and return the events until the session goes idle."""
    for task in tasks or (TaskSpec(prompt="Add a flag.", settings=Settings(model="opus")),):
        await send(session, AddTask(task=task))
    await send(session, RunTasks())
    return await events_until(session, idle(), skip=(SettingsChanged, VerificationChanged))


@dataclass(frozen=True)
class Stop:
    """A test of the event to stop at, named by ``description``."""

    description: str
    matches: Callable[[Event], bool]

    def __call__(self, event: Event) -> bool:
        return self.matches(event)

    def __repr__(self) -> str:
        return self.description


def idle(lane: BusyLane | None = "work") -> Stop:
    """Stops when ``lane`` goes idle, or any lane if ``lane`` is None."""
    return Stop(
        f"idle(lane={lane!r})",
        lambda e: isinstance(e, BusyChanged) and not e.busy and lane in (None, e.lane),
    )


def agent_exited(agent_id: str) -> Stop:
    """Stops when agent ``agent_id`` exits."""
    return Stop(
        f"agent_exited({agent_id!r})",
        lambda e: isinstance(e, AgentExited) and e.agent_id == agent_id,
    )


def all_of(*stops: Stop) -> Stop:
    """Stops once each of ``stops`` has matched an event, in any order."""
    left = list(stops)

    def matches(event: Event) -> bool:
        left[:] = [stop for stop in left if not stop(event)]
        return not left

    return Stop(f"all_of{stops!r}", matches)


def of_type(kind: type) -> Stop:
    """Stops at the first event of type ``kind``."""
    return Stop(f"of_type({kind.__name__})", lambda e: isinstance(e, kind))


async def events_until(
    session: Session | Backend,
    done: Callable[[Event], bool],
    *,
    skip: tuple[type, ...] = (),
    timeout: float = 5,
) -> list[Event]:
    """The session's events up to and including the first that's ``done``, leaving out those
    of the ``skip`` types; fails if none comes within ``timeout`` seconds."""
    events: list[Event] = []

    async def collect() -> None:
        async for event in payloads(session):
            if isinstance(event, skip):
                continue
            events.append(event)
            if done(event):
                return

    try:
        await asyncio.wait_for(collect(), timeout)
    except TimeoutError:
        raise AssertionError(f"no event matched {done!r} within {timeout}s") from None
    return events


MARK_ID = "mark"
"""The id :func:`until_marked` sends its ``History`` under."""


def _marks(event: Event) -> bool:
    """Whether ``event`` answers the ``History`` :func:`until_marked` sends."""
    refused = isinstance(event, CommandRefused) and event.command_id == MARK_ID
    return refused or isinstance(event, HistoryPage)


async def until_marked(session: WorkspaceHost) -> list[Event]:
    """The session's events up to the answer to a ``History`` sent now, which marks the end of
    those published before it: its page, or its refusal while the session is halted."""
    await greeted(session)
    mark = History(session=session.saved_session.id, before_seq=1, limit=1)
    await frontend(session).send(CommandEnvelope(command_id=MARK_ID, payload=mark))
    return await events_until(session, Stop("marked", _marks))


async def listed(session: WorkspaceHost) -> tuple[SessionSummary, ...]:
    """The saved sessions, as ``session``'s catalog lists them now, but for the open one if it
    isn't saved."""
    return await session.saved_session.summaries()


def sole[E: Event](events: list[Event], kind: type[E]) -> E:
    """The one event of type ``kind`` in ``events``."""
    [found] = [event for event in events if isinstance(event, kind)]
    return found


def board(events: list[Event]) -> tuple[TaskState, ...]:
    return [e for e in events if isinstance(e, TasksChanged)][-1].tasks


def ended_unverified(events: list[Event], task_id: str) -> TaskState:
    """Task ``task_id`` as the board first showed it ended unverified."""
    return next(
        t
        for e in events
        if isinstance(e, TasksChanged)
        for t in e.tasks
        if t.id == task_id and t.status == "unverified"
    )


def replies(events: list[Event]) -> list[str]:
    return [e.text for e in events if isinstance(e, AgentReply)]


# The only helpers that reach into a session's private state, so its refactors touch only them.


_PLANES: WeakKeyDictionary[WorkspaceHost, ControlPlane] = WeakKeyDictionary()


def served(host: WorkspaceHost, usage: UsageSource | None = None) -> ControlPlane:
    """A control plane around ``host``, fetching usage from ``usage``, if given, which the
    helpers here drive ``host`` through from now on."""
    _PLANES[host] = ControlPlane([host], usage)
    return _PLANES[host]


def control_plane(session: Backend) -> ControlPlane:
    """``session``, if it's a control plane, else the one around it the helpers here drive it
    through, made on first use."""
    if isinstance(session, ControlPlane):
        return session
    return _PLANES[session] if session in _PLANES else served(session)


class SoleFrontend:
    """The one frontend of ``plane``, as orbit's in-process one is: a ``Session`` over a
    connection to it, whose closing closes the control plane."""

    def __init__(self, plane: ControlPlane) -> None:
        self.plane = plane
        self.connection = plane.connect()

    async def send(self, envelope: CommandEnvelope) -> None:
        await self.connection.send(envelope)

    def events(self) -> AsyncIterator[EventEnvelope]:
        return self.connection.events()

    async def close(self) -> None:
        await self.plane.close()


_FRONTENDS: WeakKeyDictionary[ControlPlane, SoleFrontend] = WeakKeyDictionary()


def frontend(session: Session | Backend) -> Session:
    """``session``, or, for a :data:`Backend`, the :class:`SoleFrontend` the helpers here drive
    it through, made on first use."""
    if isinstance(session, WorkspaceHost | ControlPlane):
        return _sole_frontend(session)
    return session


def _sole_frontend(session: Backend) -> SoleFrontend:
    """The :class:`SoleFrontend` of ``session``'s control plane, made on first use."""
    plane = control_plane(session)
    if plane not in _FRONTENDS:
        _FRONTENDS[plane] = SoleFrontend(plane)
    return _FRONTENDS[plane]


def connect(session: Backend) -> SessionConnection:
    """A new connection to ``session``'s control plane."""
    return control_plane(session).connect()


async def close(session: Backend) -> None:
    """Close ``session``'s control plane, which closes its host and ends its connections."""
    await control_plane(session).close()


def queued(session: Backend) -> list[Event]:
    """Take the events the session has queued for its frontend but not yet published."""
    return [envelope.payload for envelope in queued_envelopes(session)]


def queued_envelopes(session: Backend) -> list[EventEnvelope]:
    """Take the envelopes the session has queued for its frontend but not yet published."""
    queue = _queue_of(session)
    return [queue.get_nowait() for _ in range(queue.qsize())]


async def greeted(session: Session | Backend) -> None:
    """Greet ``session``, if it's a :data:`Backend` that hasn't been."""
    if isinstance(session, WorkspaceHost | ControlPlane) and not _handshake_of(session).welcomed:
        await greet(session)


def nothing_queued(session: Backend) -> bool:
    """Whether the session has no events queued for its frontend."""
    return _queue_of(session).empty()


def _queue_of(session: Backend) -> asyncio.Queue[EventEnvelope]:
    """The events queued for the frontend of ``session``."""
    return _sole_frontend(session).connection._queue


def _handshake_of(session: Backend) -> Handshake:
    """The handshake of the frontend of ``session``."""
    return _sole_frontend(session).connection._handshake


async def save_used(session: WorkspaceHost) -> None:
    """Mark ``session`` used and save it, so its logged events outlast its closing."""
    session._keeper.use()
    await session._save()


async def publish(session: WorkspaceHost, event: Event) -> None:
    """Queue ``event`` for the session to publish, as its parts do."""
    await session._events.put(event)


async def publish_agents(session: WorkspaceHost, event: Event) -> None:
    """Have the session publish ``event`` as its agent desk does, counting what it reports."""
    await session._publish_agents(event)


def halt_of(session: WorkspaceHost) -> StorageHalt:
    """The session's storage halt, which halts it as a write fails."""
    return session._halt


async def resume_storage(session: WorkspaceHost) -> None:
    """Resume the session's storage as ``ResumeStorage`` does, with no connection attached."""
    await session._resume.resume()


def retry_of(session: WorkspaceHost) -> Retry:
    """What the session's storage halt retries as it resumes."""
    return session._halt._retry


def install_retry(session: WorkspaceHost, retry: Retry) -> None:
    """Have the session's storage halt retry ``retry`` as it resumes."""
    session._halt._retry = retry


def install_greeting(session: WorkspaceHost, greeting: HostGreeting) -> None:
    """Have the session greet the connections that attach with ``greeting``."""
    session._greeting = greeting


def review_count_of(session: WorkspaceHost) -> ReviewCount:
    """The session's review count, which loads and saves the super review count."""
    return session._review_count


def count_loader_of(session: WorkspaceHost) -> Background:
    """The background work loading the session's review count."""
    return session._count_loader


def work_lane_of(session: WorkspaceHost) -> WorkLane | None:
    """The session's work lane, if it runs work."""
    return session._work


def switcher_of(session: WorkspaceHost) -> SessionSwitcher:
    """The session's switcher, which opens and switches its sessions."""
    return session._switcher


def lane_of(session: WorkspaceHost) -> ReviewLane:
    """The session's review lane, which it must have."""
    assert session._lane is not None
    return session._lane


def commands_of(session: WorkspaceHost) -> WorkCommands:
    """The session's work commands, which queue work while it's read-only."""
    return session._commands


def turns_of(session: WorkspaceHost) -> WorkTurns:
    """The session's work lane turns, which start runs."""
    return session._turns


def tasks_of(session: WorkspaceHost) -> Tasks:
    """The session's tasks, which save and publish the board as it changes."""
    return session._tasks


def board_of(session: WorkspaceHost) -> TaskBoard:
    """The session's task board."""
    return session._board


def desk_of(session: WorkspaceHost) -> AgentDesk | None:
    """The session's agent desk, if it runs agents."""
    return session._desk


def startup_of(session: WorkspaceHost) -> StartupCheck | None:
    """The check the session runs at startup, if any."""
    return session._startup


def planner_turn(session: WorkspaceHost) -> asyncio.Task[object] | None:
    """The planner's current turn, if it has one."""
    return session._seat.turn


def store_of(session: WorkspaceHost) -> SessionStore | None:
    """Where the session is saved, if anywhere."""
    return session.saved_session._store


async def snapshot_of(session: WorkspaceHost) -> Snapshot:
    """The snapshot of the session as it stands now."""
    return (await session._snapshot()).snapshot


def put_on_board(session: WorkspaceHost, *tasks: TaskSpec) -> None:
    """Put ``tasks`` on the session's board without publishing it, as if it opened with them."""
    for task in tasks:
        session._board.add(task)


async def fetch_usage_unseen(session: Backend) -> None:
    """Have the session's usage monitor fetch once before Hello, dropping what it published."""
    monitor = control_plane(session)._usage
    assert monitor is not None
    monitor.start()
    await eventually(lambda: monitor.latest is not None, poll=0)
    queued(session)


def notice(seq: int, session: str | None = "a", message: str | None = None) -> EventEnvelope:
    """A notice saying ``message``, by default ``n<seq>``, in an envelope at ``seq`` of
    ``session``."""
    return EventEnvelope(
        seq=seq, session=session, payload=Notice(message=f"n{seq}" if message is None else message)
    )


async def claims_done(call: Call, summary: str = "done") -> TurnEndReason:
    """Fetches the task and claims it done with ``summary``."""
    await call("get_task")
    await call("complete_task", summary=summary)
    return "completed"


async def gives_up(call: Call, reason: str = "blocked") -> TurnEndReason:
    """Fetches the task and gives up on it for ``reason``."""
    await call("get_task")
    await call("give_up", reason=reason)
    return "completed"


async def no_usage() -> UsageFetch:
    """No usage: it's unavailable."""
    return UsageUnavailable("offline")


async def keeps_claiming(call: Call) -> TurnEndReason:
    await call("get_task")
    while "another agent" not in (await call("complete_task", summary="try")).text:
        pass
    return "completed"


def no_steps() -> Verification:
    """Verification turned on with no steps configured."""
    return Verification((), Steps())


class Configurator:
    """A configurator that fetches its task, records it, and stops without proposing."""

    def __init__(self) -> None:
        self.tasks: list[str] = []

    async def __call__(self, call: Call) -> TurnEndReason:
        self.tasks.append((await call("get_task")).text)
        return "completed"


async def until_idle(session: WorkspaceHost, lane: BusyLane | None = "work") -> list[Event]:
    """The events ``session`` sends until ``lane`` goes idle, or any lane if it's None."""
    return await events_until(session, idle(lane))


class GatedLog(FakeStore):
    """A log whose appends wait for ``gate``, with ``waiting`` set while one does."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.waiting = asyncio.Event()

    async def append(self, session_id: str, envelopes: Sequence[EventEnvelope]) -> int:
        self.waiting.set()
        await self.gate.wait()
        return await super().append(session_id, envelopes)


def delivered(session: WorkspaceHost) -> list[EventEnvelope]:
    """The envelopes the session has queued for the frontend, taken from its queue."""
    return queued_envelopes(session)


def payloads_of[T](kind: type[T], sent: Sequence[EventEnvelope]) -> list[T]:
    """The payloads of ``sent`` that are ``kind``s, in order."""
    return [envelope.payload for envelope in sent if isinstance(envelope.payload, kind)]


async def proposes_two(call: Call) -> TurnEndReason:
    first = await call("propose_task", title="Add the flag", prompt="Add --fast to the CLI.")
    assert "draft" in first.text
    await call("propose_task", title="Document it", prompt="Mention --fast in the README.")
    return "completed"


async def chats(call: Call) -> TurnEndReason:
    return "completed"


async def plan(session: WorkspaceHost, text: str) -> list[Event]:
    await send(session, SendMessage(agent=PLANNER, text=text))
    return await until_idle(session, None)


def steps_chat(
    *turns: Script, published: list[Event] | None = None, roles: Mapping[str, Role] = ROLES
) -> tuple[PlannerChat, ScriptedLauncher, list[tuple[str, ...]]]:
    """A planner chat playing ``turns`` with ``roles``, whose steps are the last in the list it
    returns; what it publishes goes to ``published``."""
    agents, launcher = agents_for(roles=roles)
    launcher.planner_turns = list(turns)
    sink = published if published is not None else []

    async def publish(event: Event) -> None:
        sink.append(event)

    desk = AgentDesk(agents, publish)
    steps: list[tuple[str, ...]] = [("Ruff lint",)]
    return PlannerChat(desk, Tasks(TaskBoard(), publish), lambda: steps[-1]), launcher, steps


async def hangs(call: Call) -> TurnEndReason:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


def gated_planner(gate: asyncio.Event) -> Script:
    async def ponders(call: Call) -> TurnEndReason:
        await gate.wait()
        return "completed"

    return ponders


SETTLED = {"completed", "unverified", "gave_up", "error", "cancelled"}


def suggests(*titles: str, replies: list[Reply] | None = None) -> Script:
    async def review(call: Call) -> TurnEndReason:
        await call("get_task")
        for title in titles:
            reply = await call("suggest_task", title=title, prompt=f"Please {title.lower()}.")
            if replies is not None:
                replies.append(reply)
        await call("complete_task", summary="Reviewed")
        return "completed"

    return review


async def settled() -> None:
    """Yield to the loop a while, so background work gets as far as it can."""
    for _ in range(20):
        await asyncio.sleep(0)


SETTLING = 10
"""How long :func:`settle` waits at the most, in seconds."""


async def settle(
    session: Backend,
    events: list[Event] | None = None,
    until: Callable[[list[Event]], bool] | None = None,
) -> list[Event]:
    """Events until ``until`` holds (by default, every task has finished) and nothing has
    happened for a moment; raises :class:`TimeoutError` if that takes over :data:`SETTLING`
    seconds."""
    events = [] if events is None else events

    async def pump() -> None:
        async for event in payloads(session):
            events.append(event)

    pumping = asyncio.ensure_future(pump())
    try:
        await _quiet(events, until or _finished)
        return events
    finally:
        pumping.cancel()


async def _quiet(events: list[Event], done: Callable[[list[Event]], bool]) -> None:
    """Wait until ``done`` holds for ``events`` and none has come since the event loop went
    quiet, or for 0.15 s, for :data:`SETTLING` seconds at the most."""
    async with asyncio.timeout(SETTLING):
        while True:
            seen = len(events)
            went_quiet = await quiet_within(0.15)
            if len(events) != seen:
                continue
            if done(events):
                return
            if went_quiet:  # Nothing comes by itself: look again a little later, as before.
                await asyncio.sleep(0.05)


def _finished(events: list[Event]) -> bool:
    tasks = [e for e in events if isinstance(e, TasksChanged)]
    return bool(tasks) and all(t.status in SETTLED for t in tasks[-1].tasks)


async def start(session: WorkspaceHost, *prompts: str) -> None:
    for prompt in prompts or ("Make it faster.",):
        await send(session, AddTask(task=TaskSpec(prompt=prompt)))
    await send(session, RunTasks())


LINT = VerificationStep(name="Lint", command=("ruff", "check"))


def checked(steps: Steps) -> Verification:
    return Verification((LINT, STEP), steps)


async def outcome_of(
    session: WorkspaceHost, command: ResumeStorage | RunTasks | History
) -> list[EventEnvelope]:
    """The envelopes the session sends once ``command`` is sent, up to its outcome."""
    await send(session, command)
    sent: list[EventEnvelope] = []

    async def collect() -> None:
        async for envelope in frontend(session).events():
            sent.append(envelope)
            payload = envelope.payload
            if isinstance(payload, CommandAccepted | CommandRefused) and (
                payload.command_id == COMMAND_ID
            ):
                return

    await asyncio.wait_for(collect(), 5)
    return sent


OWNER = WorkspaceOwner(pid=42)


class FakeLock:
    """A repository's lock, held by another orbit until it's ``free``; ``failing`` makes
    taking it fail."""

    def __init__(self, *, free: bool = False) -> None:
        self.free = free
        self.held = False
        self.failing = False
        self.tries = 0

    def take(self) -> WorkspaceOwner | None:
        self.tries += 1
        if self.failing:
            raise OSError("No space left on device")
        if not self.free:
            return OWNER
        self.free, self.held = False, True
        return None

    def close(self) -> None:
        if self.held:
            self.free, self.held = True, False


def statuses(events: list[Event]) -> list[str]:
    return [t.status for t in board(events)]


LIMITS = (UsageLimit(label="Session", percent=52.0),)


class Source:
    """Returns (or raises) ``outcomes`` in turn, waiting for ``gate`` first if set; once
    they run out, a fetch never ends."""

    def __init__(self, *outcomes: UsageFetch | Exception) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0
        self.gate: asyncio.Event | None = None

    async def __call__(self) -> UsageFetch:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if not self.outcomes:
            await asyncio.Event().wait()
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def logged_with_traceback(
    records: Sequence[logging.LogRecord], message: str, *, reported: bool = True
) -> bool:
    """Whether ``message`` is among ``records`` logged as an error with its traceback, marked
    ``reported`` or, if not ``reported``, unmarked."""
    return any(
        r.levelno == logging.ERROR
        and r.getMessage() == message
        and r.exc_info is not None
        and getattr(r, "reported", False) is reported
        for r in records
    )
