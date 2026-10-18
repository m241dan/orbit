"""Fakes for running tasks through agents: a host and scripted agents stand in for MCP and the
CLI, and a step runner and workspace for verification."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from orbit.agent import Agents, AgentSession, Verification, VerificationStep
from orbit.agent.application.ports import (
    AgentLaunch,
    CallHandler,
    Merge,
    Reply,
    SessionStore,
    StoredSession,
    UsageSource,
)
from orbit.agent.application.review_lane import ReviewLane
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.checks import Setup
from orbit.agent.domain.roles import REVIEW_ROLES, built_in_roles
from orbit.agent.domain.sessions import SessionRecord, Unreadable
from orbit.protocol import (
    AGENT_FAILED,
    BUILT_IN_ROLE_NAMES,
    PROTOCOL_VERSION,
    AddTask,
    AgentExited,
    AgentReply,
    AssistantMessage,
    BusyChanged,
    BusyLane,
    Catalog,
    Command,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    ErrorOccurred,
    Event,
    Hello,
    RunTasks,
    Session,
    Settings,
    SettingsChanged,
    Snapshot,
    TasksChanged,
    TaskSpec,
    TaskState,
    TurnEndReason,
    TurnFinished,
    TurnStarted,
    Usage,
    VerificationChanged,
    Welcome,
)

ROLES = built_in_roles({name: f"You are orbit's {name}." for name in BUILT_IN_ROLE_NAMES})
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
            raise RuntimeError("Orbit's MCP server failed to start.")
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
        # What each planner turn does after its TurnFinished, a turn at a time.
        self.planner_afters: list[Callable[[], Awaitable[None]]] = []
        # The usage of each launch's turns, one launch at a time; one unmetered turn if none.
        self.usages: list[tuple[Usage, ...]] = []
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
        yield TurnStarted(turn_id=agent.id, agent_id=agent.id)
        yield AssistantMessage(turn_id=agent.id, text="on it", agent_id=agent.id)

        async def call(tool: str, **arguments: Any) -> Reply:
            return await self.host.call(agent.token, tool, **arguments)

        reason = await script(call)
        if self.turn_errors:
            yield ErrorOccurred(
                turn_id=agent.id, code=AGENT_FAILED, message=self.turn_errors.pop(0)
            )
        for usage in self.usages.pop(0) if self.usages else (None,):
            yield TurnFinished(turn_id=agent.id, reason=reason, agent_id=agent.id, usage=usage)

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


class FakeStore:
    """Saved sessions kept in memory, holding the super review ``count``.

    ``failure``, while set, is the error every session save raises, and ``count_failure`` the
    error every count save raises; ``count_yields`` lists how many times each count save in
    turn yields before it's written (none once it runs out). ``free`` says whether the lock can
    be taken, and ``lock_failure``, if set, is the error taking it raises. Sessions in
    ``elsewhere`` are locked by another orbit, those in ``broken`` are unreadable for the reason
    given, and ``delete_failure``, if set, is the error deleting one raises, and
    ``release_failure`` the error letting go of a lock raises, and ``load_failure`` the error
    loading one raises. ``on_acquire``, if set, is awaited
    as a lock is taken.
    """

    def __init__(self, count: int = 0) -> None:
        self.saved: list[SessionRecord] = []
        self.locked: set[str] = set()
        self.released: list[str] = []
        self.count = count
        self.counts: list[int] = []  # Each count saved.
        self.failure: str | None = None
        self.count_failure: str | None = None
        self.count_yields: list[int] = []
        self.free = True
        self.lock_failure: str | None = None
        self.elsewhere: set[str] = set()
        self.broken: dict[str, str] = {}
        self.deleted: list[str] = []
        self.delete_failure: str | None = None
        self.release_failure: str | None = None
        self.load_failure: str | None = None
        self.on_acquire: Callable[[], Awaitable[None]] | None = None

    @property
    def last(self) -> SessionRecord:
        return self.saved[-1]

    async def save(self, record: SessionRecord) -> None:
        if self.failure is not None:
            raise RuntimeError(self.failure)
        await asyncio.sleep(0)  # Saving takes a while, as writing a file does.
        self.saved.append(record)

    async def load(self, session_id: str) -> SessionRecord | Unreadable:
        if self.load_failure is not None:
            raise RuntimeError(self.load_failure)
        if session_id in self.broken:
            return Unreadable(self.broken[session_id])
        found = [r for r in self.saved if r.id == session_id]
        return found[-1] if found else Unreadable("it doesn't exist")

    async def sessions(self) -> list[StoredSession]:
        latest: dict[str, SessionRecord | Unreadable] = {r.id: r for r in self.saved}
        latest |= {i: Unreadable(reason) for i, reason in self.broken.items()}
        return [StoredSession(i, r, i in self.elsewhere) for i, r in sorted(latest.items())]

    async def delete(self, session_id: str) -> None:
        if self.delete_failure is not None:
            raise RuntimeError(self.delete_failure)
        self.deleted.append(session_id)
        self.saved = [r for r in self.saved if r.id != session_id]
        self.locked.discard(session_id)

    async def acquire(self, session_id: str) -> bool:
        if self.on_acquire is not None:
            await self.on_acquire()
        if self.lock_failure is not None:
            raise RuntimeError(self.lock_failure)
        if session_id in self.elsewhere:
            return False
        if self.free:
            self.locked.add(session_id)
        return self.free

    async def release(self, session_id: str) -> None:
        if self.release_failure is not None:
            raise RuntimeError(self.release_failure)
        self.released.append(session_id)
        self.locked.discard(session_id)

    async def review_count(self) -> int:
        return self.count

    async def save_review_count(self, count: int) -> None:
        if self.count_failure is not None:
            raise RuntimeError(self.count_failure)
        for _ in range(self.count_yields.pop(0) if self.count_yields else 0):
            await asyncio.sleep(0)  # Saving takes a while, as writing a file does.
        self.count = count
        self.counts.append(count)


STEP = VerificationStep(name="Unit tests", command=("pytest",))


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
            raise RuntimeError(self.broken)
        return self.path

    async def commit(self, message: str) -> bool:
        if self.empty:
            return False
        self.commits.append(message)
        return True

    async def integrate(self) -> Merge:
        return self.merges.pop(0) if self.merges else Merge("merged")

    async def head(self) -> str:
        if not self.prepared:  # Like git in a worktree that doesn't exist yet.
            raise RuntimeError("git rev-parse failed: the coder's tree doesn't exist")
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

    async def untracked(self) -> frozenset[str]:
        return self.untracked_sets.pop(0) if self.untracked_sets else frozenset()

    async def ignore(self, paths: Collection[str]) -> None:
        self.ignored.append(set(paths))

    async def leftovers(self) -> list[str]:
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


def session_for(
    *scripts: Script,
    verification: Verification | None = None,
    reviewers: Sequence[Script] = (),
    check_at_startup: bool = False,
    usage: UsageSource | None = None,
    store: SessionStore | None = None,
    **options: Any,
) -> tuple[AgentSession, FakeHost, ScriptedLauncher]:
    """A session whose tasks run through agents playing ``scripts``, one per launch, and
    whose reviews run through agents playing ``reviewers``, with usage from ``usage``. It's
    saved in ``store``, if given, as ``session-1``, on a :class:`Ticks` clock.

    ``options`` go to :class:`Agents`, such as ``limits``, ``tree`` or ``roles`` (:data:`ROLES`
    unless given).
    """
    host = FakeHost()
    launcher = ScriptedLauncher(host, *scripts, reviewers=reviewers)
    tokens = iter(f"token-{n}" for n in range(1, 100))
    options = {"limits": Limits(), "exit_grace": 0.5, "roles": ROLES, **options}
    agents = Agents(host, launcher, lambda: next(tokens), **options)
    session = AgentSession(
        verification=verification,
        agents=agents,
        check_at_startup=check_at_startup,
        usage=usage,
        store=store,
        clock=Ticks(),
        new_id=lambda: "session-1",
    )
    return session, host, launcher


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


async def greet(session: Session) -> None:
    """Open ``session``'s stream with Hello, as a frontend does before anything else."""
    await session.send(CommandEnvelope(command_id=HELLO_ID, payload=HELLO))


async def send(session: Session, command: Command) -> None:
    """Send ``command`` to ``session``, in an envelope for the open session, greeting an
    :class:`AgentSession` first if it hasn't been."""
    await greeted(session)
    await session.send(CommandEnvelope(command_id=COMMAND_ID, payload=command))


async def payloads(session: Session) -> AsyncIterator[Event]:
    """The events in ``session``'s envelopes but for its ``Welcome`` and its catalogs, greeting
    an :class:`AgentSession` first if it hasn't been."""
    await greeted(session)
    async for envelope in session.events():
        if not isinstance(envelope.payload, Welcome | Catalog):
            yield envelope.payload


async def run(session: AgentSession, *tasks: TaskSpec) -> list[Event]:
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
    session: AgentSession,
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


def queued(session: AgentSession) -> list[Event]:
    """Take the events the session has queued but not yet published."""
    events: list[Event] = []
    while not session._events._queue.empty():
        events.append(session._events._queue.get_nowait().payload)
    return events


async def greeted(session: Session) -> None:
    """Greet ``session``, if it's an :class:`AgentSession` that hasn't been."""
    if isinstance(session, AgentSession) and not session._welcomed:
        await greet(session)


def nothing_queued(session: AgentSession) -> bool:
    """Whether the session has no events queued to publish."""
    return session._events._queue.empty()


async def publish(session: AgentSession, event: Event) -> None:
    """Queue ``event`` for the session to publish, as its parts do."""
    await session._events.put(event)


def lane_of(session: AgentSession) -> ReviewLane:
    """The session's review lane, which it must have."""
    assert session._lane is not None
    return session._lane


def planner_turn(session: AgentSession) -> "asyncio.Task[object] | None":
    """The planner's current turn, if it has one."""
    return session._seat.turn


def store_of(session: AgentSession) -> SessionStore | None:
    """Where the session is saved, if anywhere."""
    return session.saved_session._store


def snapshot_of(session: AgentSession) -> Snapshot:
    """The snapshot of the session as it stands now."""
    return session._snapshot()


def put_on_board(session: AgentSession, *tasks: TaskSpec) -> None:
    """Put ``tasks`` on the session's board without publishing it, as if it opened with them."""
    for task in tasks:
        session._board.add(task)


async def fetch_usage_unseen(session: AgentSession) -> None:
    """Have the session's usage monitor fetch once before Hello, dropping what it published."""
    monitor = session._usage
    assert monitor is not None
    monitor.start()
    while monitor.latest is None:
        await asyncio.sleep(0)
    queued(session)
