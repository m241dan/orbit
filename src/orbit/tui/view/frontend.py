"""The app's decisions, without the widgets and screens that show them: what it shows of each
event, what the user's keys and prompts send, and which window they move to."""

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from functools import partial
from typing import Protocol

from orbit import __version__
from orbit.protocol import (
    BUSY,
    CONFIGURE_CHECKS,
    INTERNAL,
    PROTOCOL_VERSION,
    SUPER_REVIEW,
    UNKNOWN,
    WORK_LANE,
    AcceptTask,
    AddTask,
    BusyLane,
    CancelAgent,
    CancelTurn,
    ChecksProposed,
    Command,
    DecideApproval,
    DecideProposal,
    EditTask,
    ErrorOccurred,
    Event,
    FireTrigger,
    Hello,
    RemoveTask,
    ResetConversation,
    RunTasks,
    SendMessage,
    Settings,
    TaskSpec,
    TaskState,
    UpdateSettings,
    Usage,
)
from orbit.tui.view.actions import ActionName, action
from orbit.tui.view.agents import AgentsList
from orbit.tui.view.approvals import ApprovalQueue, ApprovalsView
from orbit.tui.view.commands import CommandDesk, Send
from orbit.tui.view.common import EntryKind
from orbit.tui.view.confirm import (
    CONFIGURE_MESSAGE,
    CONFIGURE_TITLE,
    CONFIGURE_YES,
    RESET_YES,
    reset_message,
    reset_title,
)
from orbit.tui.view.events import coalesce
from orbit.tui.view.feed import ConversationFeed, Entries
from orbit.tui.view.history import HistoryPager, PagingView
from orbit.tui.view.log import Entry
from orbit.tui.view.navigation import Navigation, Views
from orbit.tui.view.opening import Opening
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.panels import VerificationSteps
from orbit.tui.view.place import PlaceView
from orbit.tui.view.prompt import PromptMarks
from orbit.tui.view.proposals import ProposalQueue, ProposalsView, decision_entry
from orbit.tui.view.reports import ReportShelf
from orbit.tui.view.router import (
    AppHooks,
    EventRouter,
    Handlers,
    Panes,
    StatusPane,
    TranscriptPane,
    UsagePane,
)
from orbit.tui.view.settings import Picked, Pickers
from orbit.tui.view.spend import UsageCorner
from orbit.tui.view.storage import StorageHalt
from orbit.tui.view.tab_strip import TabStrip
from orbit.tui.view.takeover import TakeoverOffer
from orbit.tui.view.task_editor import TaskChoices
from orbit.tui.view.tasks import TaskBoard, edit_refusal
from orbit.tui.view.transcript import busy_note, role_label, warning_entry
from orbit.tui.view.windows import SIDEBAR_WINDOWS, Windows
from orbit.tui.view.workspace_list import WorkspaceListView

Saved = Callable[[TaskSpec | None], Awaitable[None]]
"""Takes the task the editor saved, or none if it was discarded."""

Choose = Callable[[Callable[[], Settings], Picked], None]
"""Opens a settings picker on the settings it reads, passing on those picked."""


class AppView(Protocol):
    """What only the app itself does: show a warning, ask, open the task editor, label the
    trigger keys, and follow which window is selected and what has the focus."""

    @property
    def nothing_focused(self) -> bool:
        """Whether nothing has the focus: no typing, list mode or modal screen."""
        ...

    @property
    def detail_open(self) -> bool:
        """Whether the conversation's detail pane is open."""
        ...

    @property
    def sidebar_shown(self) -> bool:
        """Whether the sidebar is shown."""
        ...

    def warn(self, message: str) -> None:
        """Warn the user with ``message``."""

    def confirm(
        self, title: str, message: str, yes: str, then: Callable[[], Awaitable[object]]
    ) -> None:
        """Ask ``title`` with ``message``, ``yes`` naming what ``y`` does, and await ``then``
        only if the user answers yes."""

    def edit_task(self, task: TaskSpec | None, choices: TaskChoices, saved: Saved) -> None:
        """Open the task editor on ``task``, or a new one, offering ``choices``, the settings
        and agents now, passing on what it saves to ``saved``."""

    def label_triggers(self, labels: dict[str, str | None]) -> None:
        """Label each trigger's key with its label in ``labels``, hiding those with none."""

    def mark_selected(self, window: str, selected: bool) -> None:
        """Mark ``window`` as ``selected``, or no longer."""

    def enter(self, window: str) -> None:
        """Enter ``window``'s list, or its scroll mode, if it has one."""

    def unfocus(self) -> None:
        """Take the focus from whatever has it."""

    def show_spending(self, text: str) -> None:
        """Show ``text``, the breakdown of usage by role, in a modal."""

    def unfocus_task_list(self) -> None:
        """Take the focus from the task list, if it has it."""


class ConversationPane(Entries, TranscriptPane, Protocol):
    """The conversation, as the frontend shows events and prompts in it and the tabs switch
    what it shows."""

    @property
    def only(self) -> str | None:
        """The key of the tab whose entries alone are shown; None for All."""
        ...

    def drop_entry(self, entry: Entry) -> None:
        """Drop ``entry`` from every log holding it."""

    def show_only(self, key: str | None, label: str = "") -> None:
        """Show only the tab keyed ``key``'s entries, titled ``label``, or all."""

    def relabel(self, label: str) -> None:
        """Title the shown tab by its new ``label``."""

    def toggle_thinking(self) -> None:
        """Hide agents' dim messages in every tab, or show them again."""

    def release_cursor(self) -> None:
        """Let go of the shown log's cursor, closing the detail pane."""

    def close_detail(self) -> None:
        """Close the detail pane."""

    def show_corner(self, text: str) -> None:
        """Show ``text``, the shown tab's usage, in the corner."""


class PromptBox(Protocol):
    """The box the user types prompts to the conversational roles in."""

    @property
    def text(self) -> str:
        """What's typed in it."""
        ...

    def clear(self) -> object:
        """Empty it."""

    def normal_mode(self) -> None:
        """Stop typing in it."""

    def enter_insert_mode(self) -> None:
        """Start typing in it."""


@dataclass(frozen=True)
class Parts:
    """What the frontend shows in and asks of: the ``app``; the ``conversation``, the
    ``paging`` above it and the ``tabs`` over it; the ``prompt``, its ``marks`` and the
    ``viewing`` and ``paused`` lines by it; the sidebar's ``tasks``, ``steps``, ``usage`` and
    ``agents``; where the pickers of a session (``place``) and a workspace (``workspaces``),
    ``proposals``, consultations held for ``approvals`` and the settings ``pickers`` show; the
    storage ``halt`` and the ``takeover`` offer; the ``reports`` list; and the ``clock`` turns are
    timed by."""

    app: AppView
    conversation: ConversationPane
    paging: PagingView
    tabs: TabStrip
    prompt: PromptBox
    marks: PromptMarks
    viewing: StatusPane
    paused: StatusPane
    tasks: TaskBoard
    steps: VerificationSteps
    usage: UsagePane
    agents: AgentsList
    place: PlaceView
    workspaces: WorkspaceListView
    proposals: ProposalsView
    approvals: ApprovalsView
    pickers: Pickers
    halt: StorageHalt
    takeover: TakeoverOffer
    reports: ReportShelf
    clock: Callable[[], float]


class Frontend:
    """What the app shows and does, in its ``parts``, talking to the backend with ``send`` and
    opening as ``opening`` says, over a link if ``linked``."""

    def __init__(self, parts: Parts, send: Send, opening: Opening, linked: bool) -> None:
        self._parts = parts
        self._app = parts.app
        self.navigation = Navigation(
            Views(parts.place, parts.workspaces), self.send, opening, linked
        )
        self.desk = CommandDesk(send, self.render, lambda e: self.feed.render(e))
        self.feed = ConversationFeed(
            parts.conversation, parts.tabs, parts.tasks, parts.steps, parts.clock, self._label
        )
        self.pager = HistoryPager(parts.paging, self.feed, self.send)
        self.proposals = ProposalQueue(parts.proposals, self.decide)
        self.approvals = ApprovalQueue(parts.approvals, self.decide_approval)
        self.windows = Windows(parts.app)
        self.corner = UsageCorner(
            parts.conversation, lambda: parts.tasks.session_usage, self._task_usage
        )
        self.router = EventRouter(self._panes(), self._handlers(), self._hooks())

    def _task_usage(self, agent: str) -> Usage | None:
        """What agent ``agent``'s task has cost, if it runs one on the task list."""
        row = next((row for row in self._parts.agents.rows if row.agent_id == agent), None)
        state = None if row is None else self._parts.tasks.find(row.task_id)
        return None if state is None else state.usage

    def _label(self, role: str) -> str:
        """``role``'s label, as the workspace this frontend is on lists it."""
        navigation = self.navigation
        return role_label(role, navigation.catalog.agents(navigation.place.workspace))

    def _panes(self) -> Panes:
        parts = self._parts
        return Panes(
            parts.conversation,
            parts.tabs,
            parts.marks,
            parts.viewing,
            parts.paused,
            parts.tasks,
            parts.steps,
            parts.usage,
            parts.agents,
        )

    def _handlers(self) -> Handlers:
        parts = self._parts
        return Handlers(
            self.feed,
            self.pager,
            self.desk,
            self.proposals,
            self.approvals,
            parts.pickers,
            self.navigation,
            parts.halt,
            parts.takeover,
            parts.reports,
            self.corner,
        )

    def _hooks(self) -> AppHooks:
        app = self._app
        return AppHooks(app.label_triggers, self.leave_closed_detail, app.unfocus_task_list)

    @property
    def storage_halt(self) -> str | None:
        """Why the session is halted because storage failed; none while it isn't."""
        return self._parts.halt.explanation

    # Talking to the backend.

    async def send(
        self,
        command: Command,
        awaited: Awaited | None = None,
        failure: str = "Couldn't send the command",
    ) -> bool:
        """Whether ``command`` was sent to the open session, as :meth:`CommandDesk.send` sends
        it."""
        return await self.desk.send(command, awaited, failure)

    async def _act(self, name: ActionName, command: Command) -> bool:
        """Whether ``command`` was sent, as the user's action ``name``."""
        with action(name):
            return await self.send(command)

    def render(self, event: Event) -> None:
        """Show ``event`` wherever it concerns."""
        self.router.render(event)

    async def hello(self) -> bool:
        """Whether the Hello opening the session was sent; a failure to send it is shown."""
        return await self.send(
            Hello(client=f"orbit-tui {__version__}", protocol_version=PROTOCOL_VERSION),
            failure="The backend failed to start the session",
        )

    async def next_batch(self, batches: AsyncIterator[list[Event]]) -> list[Event]:
        """The next of ``batches``; none once they end, or once reading them fails, which is
        shown."""
        try:
            return await anext(batches, [])
        except Exception as exc:
            self.render(
                ErrorOccurred(code=INTERNAL, message=f"The backend's event stream failed: {exc}")
            )
            return []

    def show_batch(self, batch: Sequence[Event]) -> None:
        """Show ``batch``'s events, those a later one supersedes left out."""
        for event in coalesce(batch):
            self.render(event)

    async def listed(self, batch: Sequence[Event]) -> None:
        """Open the app once ``batch`` has the first catalog."""
        await self.navigation.listed(batch)

    def note_warning(self, logger: str, message: str) -> None:
        """Note the warning ``message`` from ``logger`` in the conversation."""
        self.feed.note(*warning_entry(logger, message))

    # Talking to the conversational roles and deciding on proposals.

    async def submit(self, text: str) -> None:
        """Send the prompt ``text`` to the conversational role the prompt talks to, unless it's
        blank or that role is busy, which keeps it to send again once the role is free."""
        text = text.strip()
        if not text:
            return
        seats = self.router.seats
        seat = seats.target
        if self.router.busy(seat.lane):
            self._app.warn(busy_note(seat.label, seats.answering(seat.lane)))
            return
        self._parts.prompt.normal_mode()
        # Busy from now, not from TurnStarted, so a second prompt can't race the turn.
        self.router.set_busy(seat.lane, True)
        # A newer prompt supersedes an earlier one: only its outcome frees the role.
        outcome = Awaited(
            accepted=lambda: self._sent(text),
            code=partial(self._unsend, seat.lane),
            supersedes=True,
        )
        with action("submit_prompt"):
            await self._show_and_send(
                (text, "prompt"), seat.name, SendMessage(agent=seat.name, text=text), outcome
            )

    async def _show_and_send(
        self, shown: tuple[str, EntryKind], agent: str, command: Command, awaited: Awaited
    ) -> None:
        """Show the entry ``shown`` by ``agent`` in the conversation and send ``command`` as
        ``awaited``; refused or not sent, the entry is dropped before ``awaited`` hears it."""
        entry = self.feed.append(*shown, agent)

        def dropped() -> None:
            self._parts.conversation.drop_entry(entry)
            awaited.refused()

        await self.send(command, replace(awaited, refused=dropped))

    def _sent(self, text: str) -> None:
        """Clear the prompt once its role accepted ``text``, unless the user has edited the
        draft since."""
        prompt = self._parts.prompt
        if prompt.text.strip() == text:
            prompt.clear()

    def _unsend(self, lane: BusyLane, code: str) -> None:
        """Undo sending a prompt to a conversational role answering in ``lane``, refused for the
        reason ``code`` names or not sent: the lane is freed, unless it was refused as busy, the
        prompt's text still in the prompt."""
        # Another turn holds the lane, whose end the backend reports with BusyChanged.
        if code != BUSY:
            self.router.set_busy(lane, False)

    async def decide(self, proposal: ChecksProposed, decision: DecideProposal) -> None:
        """Note the user's ``decision`` on ``proposal`` in the conversation and send it; refused
        or not sent, the note is dropped and ``proposal`` shown again, unless it was refused as
        no longer waiting."""
        shown = decision_entry(proposal.agent_id, decision)

        def undecide(code: str) -> None:
            if code != UNKNOWN:
                self.proposals.reopen(proposal)

        with action("decide_proposal"):
            await self._show_and_send(shown, proposal.agent_id, decision, Awaited(code=undecide))

    async def decide_approval(self, decision: DecideApproval) -> None:
        """Send the user's ``decision`` on a consultation held for approval."""
        await self._act("decide_approval", decision)

    # Moving between windows, which only applies with nothing focused.

    def start(self) -> None:
        """Mark the window selected at the start."""
        self.windows.start()

    def move(self, direction: str) -> None:
        """Select the window ``direction`` reaches from the one selected, with nothing
        focused."""
        app = self._app
        if app.nothing_focused:
            self.windows.move(direction, app.detail_open, app.sidebar_shown)

    def activate(self) -> None:
        """Enter the selected window's list or scroll mode; on the prompt, offer to make the
        session viewed the active one, while it's a view."""
        selected = self.windows.selected
        if selected == "prompt":
            self.navigation.place.offer()
        else:
            self._app.enter(selected)

    def release_cursor(self) -> None:
        """With the conversation selected, let go of its held cursor; with its detail pane
        selected, close the pane; with the task list selected, let go of it."""
        selected = self.windows.selected
        if selected == "transcript":
            self._parts.conversation.release_cursor()
        elif selected == "task-list":
            self._parts.tasks.let_go()
        elif selected == "detail":
            self._parts.conversation.close_detail()
            self.windows.select("transcript")

    def detail_focused(self) -> None:
        """Select the detail pane, which took the focus."""
        self.windows.select("detail")

    def insert_mode(self) -> None:
        """From any window with nothing focused, select the prompt and start typing."""
        if self._app.nothing_focused:
            self.windows.select("prompt")
            self._parts.prompt.enter_insert_mode()

    def leave_closed_detail(self) -> None:
        """Select the conversation in place of its detail pane, which has just closed."""
        if self.windows.selected == "detail":
            self.windows.select("transcript")

    def sidebar_toggled(self, shown: bool) -> None:
        """Follow the sidebar being ``shown`` or hidden: hiding it leaves a sidebar window for
        the conversation."""
        if not shown and self.windows.selected in SIDEBAR_WINDOWS:
            self._app.unfocus()
            self.windows.select("transcript")

    # The conversation's tabs.

    def tab_shown(self, tab_id: str, label: str) -> None:
        """Show the conversation of the tab ``tab_id``, labelled ``label``, which the user
        moved to, marking it read; the prompt talks to its role if it's conversational."""
        tabs = self._parts.tabs
        agent = tabs.agent_of(tab_id)
        tabs.mark_read(agent)
        self._parts.conversation.show_only(agent, label)
        conversational = any(tab.key == agent and tab.lane is not None for tab in tabs.tabs)
        self.corner.show_tab(agent, conversational)
        self.router.seats.show_tab(agent)
        self.leave_closed_detail()

    def retitle(self, key: str | None, label: str) -> None:
        """Title the conversation by ``label``, the tab keyed ``key``'s new one, if it still
        shows that tab."""
        conversation = self._parts.conversation
        if key == conversation.only:
            conversation.relabel(label)

    def cycle_conversations(self) -> None:
        """Show the planner tab, from there each other conversational role's, and from the
        last, the tab shown before."""
        self._parts.tabs.cycle_conversations()

    def show_spending(self) -> None:
        """Show the breakdown of usage by role, while a conversational role's tab is shown."""
        if self.corner.conversational:
            self._app.show_spending(self.corner.breakdown())

    def cycle_tab(self, step: int) -> None:
        """Show the next tab, or with a negative ``step``, the one before."""
        self._parts.tabs.cycle(step)

    def toggle_thinking(self) -> None:
        """Hide agents' messages while they're still thinking out loud, or show them again."""
        self._parts.conversation.toggle_thinking()

    # What the keys and lists send.

    async def run_tasks(self) -> None:
        """Ask the backend to run the task list, unless something is already running."""
        if not self.router.busy(WORK_LANE):
            await self._act("run_tasks", RunTasks())

    async def super_review(self) -> None:
        # Reviews have their own lane, so this works while tasks run.
        await self._act("super_review", FireTrigger(trigger=SUPER_REVIEW))

    def configure_checks(self) -> None:
        """Ask first, so a stray key doesn't queue a configurator."""
        # The backend queues it first and runs it, or says one is already on its way.
        command = FireTrigger(trigger=CONFIGURE_CHECKS)
        self._app.confirm(
            CONFIGURE_TITLE,
            CONFIGURE_MESSAGE,
            CONFIGURE_YES,
            lambda: self._act("configure_checks", command),
        )

    def reset_planner(self) -> None:
        """Ask first, so a stray key doesn't end the conversation of the role the prompt talks
        to."""
        # The backend refuses while the role answers, or notes there's nothing to reset.
        role = self.router.seats.target.name
        command = ResetConversation(agent=role)
        self._app.confirm(
            reset_title(role),
            reset_message(role),
            RESET_YES,
            lambda: self._act("reset_planner", command),
        )

    async def cancel_turn(self) -> None:
        # The conversational roles have their own lanes, stopped with ctrl+x; ctrl+s stops the
        # task run.
        await self._act("cancel_turn", CancelTurn(lane=WORK_LANE))

    async def stop_planner(self) -> None:
        """Stop the turn of the role the prompt talks to."""
        await self._act("stop_planner", CancelTurn(lane=self.router.seats.target.lane))

    async def cancel_agent(self, agent_id: str) -> None:
        await self._act("cancel_agent", CancelAgent(agent_id=agent_id))

    async def accept_task(self, task_id: str) -> None:
        await self._act("accept_task", AcceptTask(task_id=task_id))

    async def remove_task(self, task_id: str) -> None:
        await self._act("remove_task", RemoveTask(task_id=task_id))

    async def page_back(self) -> None:
        """Show the page of the session's history before the conversation shown, if any."""
        with action("page_history"):
            await self.pager.earlier()

    def edit_task(self, current: TaskState | None) -> None:
        """Open the editor on the task ``current``, or a new one, offering the settings shown,
        those of the role the prompt talks to, and the agents of the workspace this frontend is
        on, as the catalog lists them, unless it can't be edited, which the user is told; the
        list changes when the backend reports the new task list."""
        refusal = None if current is None else edit_refusal(current)
        if refusal is not None:
            self._app.warn(refusal)
            return

        async def saved(result: TaskSpec | None) -> None:
            if result is None:
                return
            if current is None:
                await self._act("add_task", AddTask(task=result))
            else:
                await self._act("edit_task", EditTask(task_id=current.id, task=result))

        task = None if current is None else current.task
        agents = self.navigation.catalog.agents(self.navigation.place.workspace)
        choices = TaskChoices(self.router.seats.settings or Settings(), agents)
        self._app.edit_task(task, choices, saved)

    def list_sessions(self) -> None:
        """Show the session picker, the session viewed first."""
        self.navigation.place.open()

    def list_workspaces(self) -> None:
        """Show the workspace picker."""
        self.navigation.workspaces.open()

    async def list_reports(self) -> None:
        """Show the workspace's reports."""
        await self._parts.reports.open()

    # The settings pickers, which set the settings of the role the prompt talks to, and open
    # once the backend has reported settings.

    def pick_model(self) -> None:
        self._pick(self._parts.pickers.choose_model, "pick_model")

    def pick_tool_access(self) -> None:
        self._pick(self._parts.pickers.choose_tools, "pick_tool_access")

    def pick_effort(self) -> None:
        self._pick(self._parts.pickers.choose_effort, "pick_effort")

    def _pick(self, choose: Choose, name: ActionName) -> None:
        """Have ``choose`` open its picker on the settings of the role the prompt talks to,
        requesting them with the pick as the user's action ``name``, once there are any."""
        seats = self.router.seats
        role = seats.target.name
        if seats.settings_of(role) is None:
            return

        def settings() -> Settings:
            shown = seats.settings_of(role)
            assert shown is not None  # Settings once reported are never forgotten.
            return shown

        choose(settings, partial(self._request_settings, name, role))

    async def _request_settings(self, name: ActionName, role: str, settings: Settings) -> None:
        # The display updates when the backend confirms with SettingsChanged.
        await self._act(name, UpdateSettings(settings=settings, role=role))
