"""What the frontends see of a workspace's open session, assembled from the parts that hold it:
what's in progress there, its snapshot and the workspace's entry in the catalog."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePath
from typing import Protocol, Unpack

from orbit.agent.application.lane_runner import Lanes
from orbit.agent.application.outbox import Shown, Transcript
from orbit.agent.application.proposals import FrontendReview
from orbit.agent.application.settings import SessionSettings
from orbit.agent.application.views import SessionParts
from orbit.agent.domain.activity import Activity
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.catalog import agent_entries, workspace_entry
from orbit.agent.domain.role_spend import RoleSpend
from orbit.agent.domain.roles import Role, enabled_roles
from orbit.agent.domain.verification import StepProgress
from orbit.protocol import (
    AgentEntry,
    ApprovalRequested,
    BusyLane,
    ReviewStatus,
    RunningAgent,
    SessionStateChanged,
    SessionSummary,
    Snapshot,
    StepState,
    VerificationChanged,
    WorkspaceEntry,
)


class Turns(Protocol):
    """The work lane's turns, as what's shown reads whether they run."""

    @property
    def running(self) -> bool: ...


class Work(Protocol):
    """The work lane, as what's in progress reads where its run is."""

    @property
    def current(self) -> str | None: ...

    @property
    def waiting_for_coder(self) -> int | None: ...

    @property
    def coders_busy(self) -> int | None: ...


class Reviews(Protocol):
    """The review lane, as what's shown reads it."""

    @property
    def running(self) -> bool: ...

    @property
    def reviewing(self) -> tuple[str, ...] | None: ...

    @property
    def task(self) -> str | None: ...

    @property
    def status(self) -> ReviewStatus: ...


class Runner(Protocol):
    """A lane that starts by itself, as what's in progress reads it."""

    @property
    def running(self) -> bool: ...

    @property
    def current(self) -> str | None: ...


class Seats(Protocol):
    """The conversational roles' seats, as what's shown reads them."""

    @property
    def names(self) -> tuple[str, ...]: ...

    @property
    def answering(self) -> tuple[str, ...]: ...

    @property
    def approvals(self) -> tuple[ApprovalRequested, ...]: ...

    def busy(self) -> dict[BusyLane, bool]: ...


class Startup(Protocol):
    """The startup check, as what's shown reads it."""

    @property
    def running(self) -> bool: ...

    @property
    def panel(self) -> VerificationChanged | None: ...

    @property
    def agent(self) -> RunningAgent | None: ...


class Switched(Protocol):
    """The switcher, as the snapshot reads what the open session is and spent."""

    @property
    def spend(self) -> RoleSpend: ...

    def summary(self, last_active: datetime | None) -> SessionSummary: ...


class Verifying(Protocol):
    """The verifier, as the snapshot reads how far the steps verifying tasks have got."""

    @property
    def progress(self) -> StepProgress: ...


class Launching(Protocol):
    """The agent desk, as the snapshot reads the agents running."""

    @property
    def running(self) -> tuple[RunningAgent, ...]: ...


class States(Protocol):
    """The sessions' states, as what's shown reads them."""

    def current(self, *, joining: bool = False) -> dict[str, SessionStateChanged]: ...

    def of(self, session_id: str) -> SessionStateChanged: ...


class Snapshotting(Protocol):
    """The views, as they make a snapshot of a session's own parts and the workspace's."""

    def snapshot(self, **session: Unpack[SessionParts]) -> Snapshot: ...


class Roster(Protocol):
    """The agents, as the catalog reads their roles."""

    @property
    def roles(self) -> Mapping[str, Role]: ...

    @property
    def personal(self) -> frozenset[str]: ...

    @property
    def usage_unknown(self) -> frozenset[str]: ...


@dataclass(frozen=True)
class ActivityParts:
    """What's in progress is read from: the work lane's ``turns`` and the ``work`` lane, the
    review ``lane``, the ``runners`` of the other lanes that start by themselves, the
    conversational roles' ``seats`` and the ``startup`` check; the work and review lanes and the
    startup check if there are any."""

    turns: Turns
    work: Work | None
    lane: Reviews | None
    runners: Sequence[Runner]
    seats: Seats
    startup: Startup | None


class SessionActivity:
    """Reads what's in progress in the open session from the parts in ``parts``."""

    def __init__(self, parts: ActivityParts) -> None:
        self._parts = parts

    def now(self) -> Activity:
        """What's in progress now: a task run, a review, the conversational roles answering, the
        startup check and the tasks running in the other lanes that start by themselves, with
        the tasks the run and the review are on, and where the run waits for a coder, behind how
        many busy."""
        parts = self._parts
        task_id, waiting, busy = self._work_activity()
        return Activity(
            tasks=parts.turns.running,
            review=parts.lane is not None and parts.lane.running,
            answering=parts.seats.answering,
            startup_check=parts.startup is not None and parts.startup.running,
            task_id=task_id,
            reviewing=parts.lane.reviewing if parts.lane else None,
            waiting_for_coder=waiting,
            coders_busy=busy,
            lane_tasks=self._lane_tasks(),
        )

    def _lane_tasks(self) -> tuple[str, ...]:
        """The tasks running in the lanes that start by themselves, reviews aside: the review
        lane's task of another role, then the other lanes'."""
        parts = self._parts
        review = parts.lane.task if parts.lane else None
        running = (runner.current for runner in parts.runners if runner.running)
        return tuple(task for task in (review, *running) if task is not None)

    def _work_activity(self) -> tuple[str | None, int | None, int | None]:
        """The task the run is on, where it waits for a coder and behind how many busy; none
        of them without a work lane."""
        work = self._parts.work
        if work is None:
            return None, None, None
        return work.current, work.waiting_for_coder, work.coders_busy


@dataclass(frozen=True)
class SnapshotParts:
    """What the open session's snapshot is made of: the ``session`` open's id, the
    ``transcript`` of a session's latest events, the ``switcher`` saying what the session is and
    spent, its ``settings``, the conversational roles' ``seats``, the task ``board``, the
    ``proposals`` waiting, the work lane's ``turns``, the ``lanes``, the review ``lane``, the
    ``startup`` check, the ``verifier`` and the agent ``desk``, each of these four if there is
    one, the sessions' ``states`` and the ``views`` that make the snapshot."""

    session: Callable[[], str]
    transcript: Callable[[str], Awaitable[Transcript]]
    switcher: Switched
    settings: SessionSettings
    seats: Seats
    board: TaskBoard
    proposals: FrontendReview
    turns: Turns
    lanes: Lanes
    lane: Reviews | None
    startup: Startup | None
    verifier: Verifying | None
    desk: Launching | None
    states: States
    views: Snapshotting


class SessionSnapshots:
    """Makes the open session's snapshot from the parts in ``parts``."""

    def __init__(self, parts: SnapshotParts) -> None:
        self._parts = parts

    async def snapshot(self, last_active: datetime | None = None) -> Shown:
        """The session open, last active at ``last_active``, as it stands now, with its latest
        events, and why it shows none if they can't be read."""
        parts = self._parts
        transcript = await parts.transcript(parts.session())
        # Read after the transcript, so the state is as it stands once that's read.
        steps, title = self._steps_shown()
        snapshot = parts.views.snapshot(
            session=parts.switcher.summary(last_active),
            settings=parts.settings.current,
            role_settings={role: parts.settings.of(role) for role in parts.seats.names},
            busy=self._busy(),
            tasks=parts.board.tasks,
            steps=steps,
            steps_title=title,
            review=parts.lane.status if parts.lane else None,
            proposals=parts.proposals.waiting,
            agents=self._running_agents(),
            approvals=parts.seats.approvals,
            transcript=transcript.events,
            viewers=parts.states.of(parts.session()).viewers,
            role_spent=parts.switcher.spend.by_role,
            caused_spent=parts.switcher.spend.caused,
        )
        return Shown(snapshot, transcript.error)

    def _busy(self) -> dict[BusyLane, bool]:
        """Whether each lane is busy, by name: the work lane, the conversational roles' and those
        that start by themselves."""
        parts = self._parts
        work = {parts.lanes.work: parts.turns.running}
        return work | parts.seats.busy() | parts.lanes.busy()

    def _steps_shown(self) -> tuple[tuple[StepState, ...], str]:
        """The verification steps the panel shows, and their title: the startup check's while
        it has the panel, else those verifying tasks; none without verification."""
        parts = self._parts
        startup = parts.startup.panel if parts.startup else None
        if startup is not None:
            return startup.steps, startup.title
        return (parts.verifier.progress.steps if parts.verifier else ()), ""

    def _running_agents(self) -> tuple[RunningAgent, ...]:
        """The agents running, the startup check first while it runs."""
        parts = self._parts
        startup = parts.startup.agent if parts.startup else None
        agents = parts.desk.running if parts.desk else ()
        return agents if startup is None else (startup, *agents)


def _workspace_label(workspace: str | None) -> str | None:
    """The name of the directory of the repository at ``workspace``; none without one."""
    return PurePath(workspace).name if workspace else None


@dataclass(frozen=True)
class EntryParts:
    """What the workspace's entry in the catalog is made of: its id, ``workspace_id``, and the
    path of its repository, ``workspace``, if it has one; whether it's ``managed``, a clone
    orbit made, and whether it's ``read_only`` now; the sessions' ``states``; its ``agents`` as
    they stand, if it has any; and the conversational roles' ``seats``."""

    workspace_id: str
    workspace: str | None
    managed: bool
    read_only: Callable[[], bool]
    states: States
    agents: Callable[[], Roster | None]
    seats: Seats


class CatalogEntries:
    """Makes the workspace's entry in the catalog from the parts in ``parts``."""

    def __init__(self, parts: EntryParts) -> None:
        self._parts = parts

    def entry(self, sessions: tuple[SessionSummary, ...]) -> WorkspaceEntry:
        """The workspace's entry in the catalog, holding ``sessions``, the open one's state as
        a connection joining would see it, and its agents' enabled roles."""
        parts = self._parts
        live = parts.states.current(joining=True).values()
        workspace = (parts.workspace_id, _workspace_label(parts.workspace), parts.workspace)
        entry = workspace_entry(
            workspace,
            sessions,
            read_only=parts.read_only(),
            managed=parts.managed,
            live=live,
        )
        return entry.model_copy(update={"agents": self._agents()})

    def _agents(self) -> tuple[AgentEntry, ...]:
        """The entries of the agents' enabled roles, those with a seat ones the user can
        message; none without agents."""
        agents = self._parts.agents()
        if agents is None:
            return ()
        roles = enabled_roles(agents.roles)
        return agent_entries(roles, self._parts.seats.names, agents.personal, agents.usage_unknown)
