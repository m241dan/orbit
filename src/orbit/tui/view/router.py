"""Routes each backend event to the widgets and hosts that show it."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, assert_never

from orbit.protocol import (
    PLANNER,
    PLANNER_LANE,
    STORAGE_FAILED,
    WORK_LANE,
    AgentEntry,
    AgentsChanged,
    BusyChanged,
    BusyLane,
    Catalog,
    ChecksProposed,
    CommandRefused,
    DirectoryListing,
    ErrorOccurred,
    Event,
    HistoryPage,
    MessageSent,
    OwnershipOffered,
    OwnershipTaken,
    ReviewStatus,
    SessionSummary,
    Settings,
    SettingsChanged,
    Snapshot,
    StepState,
    TasksChanged,
    TaskState,
    Usage,
    UsageChanged,
    UsageTotals,
    VerificationChanged,
    WorkspaceCloning,
)
from orbit.tui.view.commands import CommandDesk
from orbit.tui.view.events import (
    ANSWER_EVENTS,
    FEED_EVENTS,
    OWNERSHIP_EVENTS,
    SESSION_EVENTS,
    STATE_EVENTS,
    AnswerEvent,
    OwnershipEvent,
    ProgressEvent,
    SessionEvent,
    StateEvent,
    in_group,
)
from orbit.tui.view.feed import ConversationFeed
from orbit.tui.view.history import HistoryPager
from orbit.tui.view.navigation import Navigation
from orbit.tui.view.rules import reset_entry
from orbit.tui.view.seats import Seats
from orbit.tui.view.storage import StorageHalt
from orbit.tui.view.takeover import TakeoverOffer
from orbit.tui.view.tasks import role_lanes, usage_unknown_roles
from orbit.tui.view.triggers import trigger_key_labels


class TranscriptPane(Protocol):
    """The conversation window, as the router shows events in it."""

    def track(self, event: Event, roles: Mapping[str, str]) -> None: ...

    def show_session(self, name: str | None) -> None: ...


class TabsPane(Protocol):
    """The conversation's tabs, as the router shows events in them."""

    @property
    def roles(self) -> Mapping[str, str]: ...

    def track(self, event: Event) -> None: ...

    def pin(self, lanes: Mapping[str, BusyLane]) -> None: ...

    def set_busy(self, lane: BusyLane, busy: bool) -> None: ...


class PromptPane(Protocol):
    """The prompt, as the router marks it."""

    def show_target(self, role: str, label: str) -> None: ...

    def show_busy(self, busy: bool) -> None: ...

    def show_read_only(self, read_only: bool) -> None: ...

    def show_settings(self, settings: Settings) -> None: ...


class StatusPane(Protocol):
    """A line showing a status, hidden while there's none."""

    def show(self, note: str | None) -> None: ...


class TaskListPane(Protocol):
    """The task list, as the router shows the tasks in it."""

    @property
    def numbers_by_id(self) -> Mapping[str, str]: ...

    def show(self, tasks: Sequence[TaskState]) -> None: ...

    def show_lanes(self, lanes: Mapping[str, str]) -> None: ...

    def show_usage_unknown(self, roles: frozenset[str]) -> None: ...

    def show_waiting(self, waiting: tuple[str, str] | None) -> None: ...

    def widen(self) -> None: ...

    def let_go(self) -> None: ...

    def reset_usage(self, usage: Usage) -> None: ...


class StepsPane(Protocol):
    """The verification steps, as the router shows them."""

    def show(self, steps: Sequence[StepState], title: str = "") -> None: ...


class UsagePane(Protocol):
    """The usage limits, as the router shows them."""

    def show(self, event: UsageChanged) -> None: ...


class AgentsPane(Protocol):
    """The agents running, as the router shows them."""

    def track(self, event: Event) -> None: ...

    def number_tasks(self, numbers: Mapping[str, str]) -> None: ...

    def show_review(self, status: ReviewStatus | None) -> None: ...


class Proposals(Protocol):
    """The proposals waiting on the user, as the router follows them."""

    def show(self, event: ChecksProposed) -> None: ...

    def follow(self, proposals: tuple[ChecksProposed, ...]) -> None: ...

    def withdraw(self, event: Event) -> None: ...


class ModelChoices(Protocol):
    """The settings pickers, as the router offers model choices in them, with the id each
    resolves to."""

    def show_choices(self, choices: tuple[str, ...], ids: Mapping[str, str]) -> None: ...


@dataclass(frozen=True)
class Panes:
    """The app's widgets that show events: the conversation's ``transcript`` and ``tabs``, the
    ``prompt`` and, by it, the ``viewing`` mark and the line saying the run is ``paused``, and
    the sidebar's ``task_list``, ``verification`` steps, ``usage`` and ``agents``."""

    transcript: TranscriptPane
    tabs: TabsPane
    prompt: PromptPane
    viewing: StatusPane
    paused: StatusPane
    task_list: TaskListPane
    verification: StepsPane
    usage: UsagePane
    agents: AgentsPane


@dataclass(frozen=True)
class Handlers:
    """The app's hosts and helpers that follow events."""

    feed: ConversationFeed
    pager: HistoryPager
    desk: CommandDesk
    proposals: Proposals
    pickers: ModelChoices
    navigation: Navigation
    halt: StorageHalt
    takeover: TakeoverOffer


@dataclass(frozen=True)
class AppHooks:
    """What the router asks of the app itself: to ``label_triggers``' keys, to
    ``leave_closed_detail`` once the conversation's detail pane closed, and to
    ``unfocus_task_list`` if it has the focus."""

    label_triggers: Callable[[dict[str, str | None]], None]
    leave_closed_detail: Callable[[], None]
    unfocus_task_list: Callable[[], None]


class EventRouter:
    """Shows each event in the ``panes`` and the ``handlers`` it concerns, asking the app for
    what only it does through ``hooks``; it keeps which lanes are busy, as last reported, and
    the conversational roles' ``seats``, with their settings, those of the workspace this
    frontend is on."""

    def __init__(self, panes: Panes, handlers: Handlers, hooks: AppHooks) -> None:
        self._panes = panes
        self._handlers = handlers
        self._hooks = hooks
        # Which lanes have work in progress, as last reported (or just requested).
        self._busy: dict[BusyLane, bool] = {WORK_LANE: False, PLANNER_LANE: False}
        self.seats = Seats(panes.prompt, self.busy)
        self._agents: tuple[AgentEntry, ...] = ()  # The roles shown, those of no workspace.

    def busy(self, lane: BusyLane) -> bool:
        """Whether ``lane`` has work in progress, as last reported or set; a lane not reported
        yet has none."""
        return self._busy.get(lane, False)

    def render(self, event: Event) -> None:
        """Show ``event`` wherever it concerns."""
        self._follow_agents(event)
        if in_group(event, STATE_EVENTS):
            self._render_state(event)
        elif in_group(event, FEED_EVENTS):
            self._handlers.feed.render(event)
        elif in_group(event, SESSION_EVENTS):
            self._render_session(event)
        elif in_group(event, ANSWER_EVENTS):
            self._render_answer(event)
        else:
            assert_never(event)
        self._follow_halt(event)
        if isinstance(event, ErrorOccurred):
            self._handlers.navigation.follow_error(event)

    def set_busy(self, lane: BusyLane, busy: bool) -> None:
        """Note whether ``lane`` is ``busy``, showing it on the tabs of the conversational roles
        answering in it, and on the prompt if it talks to one."""
        self._busy[lane] = busy
        self._panes.tabs.set_busy(lane, busy)
        self.seats.follow_busy(lane, busy)

    def show_viewing(self) -> None:
        """Mark the prompt while the session viewed isn't the active one, or hide the mark, say
        by it while the session viewed's run is paused, and have the task about to run say when
        the session viewed waits for a coder, and follow the roles of the workspace this
        frontend is on, if it moved or they changed."""
        place = self._handlers.navigation.place
        self._panes.viewing.show(place.mark)
        self._panes.paused.show(place.paused)
        self._panes.task_list.show_waiting(place.coder_wait)
        self._follow_roles()

    def _follow_roles(self) -> None:
        """Pin a tab for each conversational role of the workspace this frontend is on, which
        the prompt talks to while its tab is shown, and tag the tasks with their roles' lanes and
        whether their usage is unknown, once those roles differ from the ones shown; before a
        workspace is open there's only the planner."""
        navigation = self._handlers.navigation
        agents = navigation.catalog.agents(navigation.place.workspace)
        if agents == self._agents:
            return
        self._agents = agents
        self.seats.follow_catalog(agents)
        self._panes.tabs.pin(self.seats.lanes)
        self._panes.task_list.show_lanes(role_lanes(agents))
        self._panes.task_list.show_usage_unknown(usage_unknown_roles(agents))

    def _render_answer(self, event: AnswerEvent) -> None:
        """Follow what the backend made of a command, noting the role a message it accepted went
        to as answering in its lane."""
        if isinstance(event, MessageSent):
            self.seats.follow_message(event.agent)
        self._handlers.desk.answer(event)

    def _render_ownership(self, event: OwnershipEvent) -> None:
        """Ask whether to take over the repository offered, or close the offer once it's
        settled, clearing the read-only mark if this orbit owns it."""
        takeover = self._handlers.takeover
        if isinstance(event, OwnershipOffered):
            takeover.offered()
            return
        takeover.settled()
        if isinstance(event, OwnershipTaken):
            self._panes.prompt.show_read_only(False)

    def _follow_halt(self, event: Event) -> None:
        """Show the halt a storage error or a command refused for it reports."""
        match event:
            case (
                ErrorOccurred(code=code, message=message)
                | CommandRefused(code=code, message=message)
            ) if code == STORAGE_FAILED:
                self._handlers.halt.halted(message)

    def _render_session(self, event: SessionEvent) -> None:
        """Rule off a conversational role's conversation where it was reset, show a snapshot of
        the session viewed or a page of its history, follow a catalog, or follow the session's
        hold on the repository."""
        if isinstance(event, Snapshot):
            self._render_snapshot(event)
        elif isinstance(event, Catalog):
            self._render_catalog(event)
        elif isinstance(event, HistoryPage):
            self._handlers.pager.show(event)
        elif in_group(event, OWNERSHIP_EVENTS):
            self._render_ownership(event)
        else:
            self._handlers.feed.append(*reset_entry(event.agent), event.agent)

    def _render_catalog(self, catalog: Catalog) -> None:
        """Label the trigger keys from ``catalog``, list its workspaces and sessions, titling
        the conversation with the name of the session viewed, renamed perhaps, and follow the
        roles of the workspace the app is on; one that no longer lists the workspace the app is
        on opens the workspace picker. In process, the first catalog opens the session picker,
        if the app picks one."""
        self._hooks.label_triggers(trigger_key_labels(catalog.triggers))
        navigation = self._handlers.navigation
        navigation.follow_catalog(catalog)
        attached = navigation.place.attached_session
        if attached is not None:
            self._panes.transcript.show_session(attached.name)
        self.show_viewing()

    def _open_session(self, session: SessionSummary) -> None:
        """Title the conversation with ``session``'s name, take the task list out of list
        mode, show all its tasks again, let it follow again and start its total from what
        ``session`` spent."""
        self._panes.transcript.show_session(session.name)
        self._hooks.unfocus_task_list()
        self._panes.task_list.widen()
        self._panes.task_list.let_go()
        self._panes.task_list.reset_usage(session.usage)

    def _render_snapshot(self, snapshot: Snapshot) -> None:
        """Replace the state shown with ``snapshot``'s, the conversation with its transcript's,
        and open the session it holds, asking about the repository while it says it's offered;
        the agents pane follows it as it follows every event, and the working row once its
        roles are known."""
        panes, handlers = self._panes, self._handlers
        handlers.pickers.show_choices(snapshot.model_choices, snapshot.model_ids)
        self.seats.replace_settings({**snapshot.role_settings, PLANNER: snapshot.settings})
        self.seats.forget_answering()
        for lane, busy in snapshot.busy.items():
            self.set_busy(lane, busy)
        self._show_tasks(snapshot.tasks)
        panes.verification.show(snapshot.steps, snapshot.steps_title)
        panes.agents.show_review(snapshot.review)
        handlers.proposals.follow(snapshot.proposals)
        # Its tasks shown first, as the transcript's notes on them read them, and its usage
        # set after, as the transcript's turns count theirs.
        handlers.feed.replace(
            (envelope.payload for envelope in snapshot.transcript), snapshot.agents
        )
        self._hooks.leave_closed_detail()
        panes.transcript.track(snapshot, panes.tabs.roles)
        handlers.pager.follow(snapshot)
        self._open_session(snapshot.session)
        handlers.navigation.view(snapshot)
        self.show_viewing()
        panes.prompt.show_read_only(snapshot.read_only)
        handlers.takeover.follow(snapshot.ownership_offered)
        if snapshot.storage_halt is None:
            handlers.halt.lifted()
        else:
            handlers.halt.halted(snapshot.storage_halt)

    def _render_state(self, event: StateEvent) -> None:
        """Show a snapshot of backend state, or a decision it waits for."""
        match event:
            case SettingsChanged():
                self._follow_settings(event)
            case BusyChanged(busy=busy, lane=lane):
                self.set_busy(lane, busy)
            case TasksChanged(tasks=tasks):
                self._show_tasks(tasks)
            case AgentsChanged() | UsageTotals() | DirectoryListing():
                self._follow_stream(event)
            case _:
                self._show_progress(event)

    def _follow_settings(self, event: SettingsChanged) -> None:
        """Offer the model choices ``event`` reports, with the id each resolves to, and note its
        role's settings, shown under the prompt while it talks to that role."""
        self._handlers.pickers.show_choices(event.model_choices, event.model_ids)
        self.seats.follow_settings(event.role, event.settings)

    def _follow_stream(self, event: AgentsChanged | UsageTotals | DirectoryListing) -> None:
        """Show the agents running across workspaces in the workspace picker, and the server's
        directories in its explorer; nothing shows what turns cost across them."""
        if isinstance(event, AgentsChanged):
            self._handlers.navigation.follow_agents(event)
        elif isinstance(event, DirectoryListing):
            self._handlers.navigation.follow_listing(event)

    def _follow_agents(self, event: Event) -> None:
        panes = self._panes
        panes.agents.track(event)
        panes.tabs.track(event)
        # A snapshot's roles are known only once it replaces the conversation.
        if not isinstance(event, Snapshot):
            panes.transcript.track(event, panes.tabs.roles)
        self._handlers.proposals.withdraw(event)

    def _show_tasks(self, tasks: tuple[TaskState, ...]) -> None:
        task_list = self._panes.task_list
        task_list.show(tasks)
        self._panes.agents.number_tasks(task_list.numbers_by_id)

    def _show_progress(self, event: ProgressEvent) -> None:
        """Show the verification steps' progress, whether review is paused, proposed steps, usage
        limits, a clone's progress or a session's state, which the pickers show."""
        if isinstance(event, VerificationChanged):
            self._panes.verification.show(event.steps, event.title)
        elif isinstance(event, ReviewStatus):
            self._panes.agents.show_review(event)
        elif isinstance(event, ChecksProposed):
            self._handlers.proposals.show(event)
        elif isinstance(event, UsageChanged):
            self._panes.usage.show(event)
        elif isinstance(event, WorkspaceCloning):
            self._handlers.navigation.follow_cloning(event)
        else:
            self._handlers.navigation.follow_state(event)
            self.show_viewing()
