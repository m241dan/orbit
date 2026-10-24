"""The app's decisions, without the widgets and screens that show them: what it shows of each
event, what the user's keys and prompts send, and which window they move to."""

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from orbit import __version__
from orbit.protocol import (
    CONFIGURE_CHECKS,
    INTERNAL,
    PLANNER,
    PLANNER_LANE,
    PROTOCOL_VERSION,
    SUPER_REVIEW,
    UNKNOWN,
    WORK_LANE,
    AcceptTask,
    AddTask,
    CancelAgent,
    CancelTurn,
    ChecksProposed,
    Command,
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
)
from orbit.tui.view.commands import CommandDesk, Send
from orbit.tui.view.common import EntryKind
from orbit.tui.view.confirm import (
    CONFIGURE_MESSAGE,
    CONFIGURE_TITLE,
    CONFIGURE_YES,
    RESET_MESSAGE,
    RESET_TITLE,
    RESET_YES,
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
from orbit.tui.view.router import (
    AgentsPane,
    AppHooks,
    EventRouter,
    Handlers,
    Panes,
    StatusPane,
    TranscriptPane,
    UsagePane,
)
from orbit.tui.view.settings import Pickers
from orbit.tui.view.storage import StorageHalt
from orbit.tui.view.tab_strip import TabStrip
from orbit.tui.view.takeover import TakeoverOffer
from orbit.tui.view.tasks import TaskBoard, edit_refusal
from orbit.tui.view.transcript import PLANNER_BUSY, warning_entry
from orbit.tui.view.windows import SIDEBAR_WINDOWS, Windows
from orbit.tui.view.workspace_list import WorkspaceListView

Saved = Callable[[TaskSpec | None], Awaitable[None]]
"""Takes the task the editor saved, or none if it was discarded."""


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

    def edit_task(self, task: TaskSpec | None, settings: Settings, saved: Saved) -> None:
        """Open the task editor on ``task``, or a new one, with ``settings``, the settings now,
        passing on what it saves to ``saved``."""

    def label_triggers(self, labels: dict[str, str | None]) -> None:
        """Label each trigger's key with its label in ``labels``, hiding those with none."""

    def mark_selected(self, window: str, selected: bool) -> None:
        """Mark ``window`` as ``selected``, or no longer."""

    def enter(self, window: str) -> None:
        """Enter ``window``'s list, or its scroll mode, if it has one."""

    def unfocus(self) -> None:
        """Take the focus from whatever has it."""

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


class PromptBox(Protocol):
    """The box the user types prompts to the planner in."""

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
    ``viewing`` line by it; the sidebar's ``tasks``, ``steps``, ``usage`` and ``agents``; where
    the pickers of a session (``place``) and a workspace (``workspaces``), ``proposals`` and
    the settings ``pickers`` show; the storage ``halt`` and the ``takeover`` offer; and the
    ``clock`` turns are timed by."""

    app: AppView
    conversation: ConversationPane
    paging: PagingView
    tabs: TabStrip
    prompt: PromptBox
    marks: PromptMarks
    viewing: StatusPane
    tasks: TaskBoard
    steps: VerificationSteps
    usage: UsagePane
    agents: AgentsPane
    place: PlaceView
    workspaces: WorkspaceListView
    proposals: ProposalsView
    pickers: Pickers
    halt: StorageHalt
    takeover: TakeoverOffer
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
            parts.conversation, parts.tabs, parts.tasks, parts.steps, parts.clock
        )
        self.pager = HistoryPager(parts.paging, self.feed, self.send)
        self.proposals = ProposalQueue(parts.proposals, self.decide)
        self.windows = Windows(parts.app)
        self.router = EventRouter(self._panes(), self._handlers(), self._hooks())

    def _panes(self) -> Panes:
        parts = self._parts
        return Panes(
            parts.conversation,
            parts.tabs,
            parts.marks,
            parts.viewing,
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
            parts.pickers,
            self.navigation,
            parts.halt,
            parts.takeover,
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

    # Talking to the planner and deciding on proposals.

    async def submit(self, text: str) -> None:
        """Send the prompt ``text`` to the planner, unless it's blank or the planner is busy,
        which keeps it to send again once the planner is free."""
        text = text.strip()
        if not text:
            return
        if self.router.busy(PLANNER_LANE):
            self._app.warn(PLANNER_BUSY)
            return
        self._parts.prompt.normal_mode()
        # Busy from now, not from TurnStarted, so a second prompt can't race the turn.
        self.router.set_busy(PLANNER_LANE, True)
        # A newer prompt supersedes an earlier one: only its outcome frees the planner.
        outcome = Awaited(accepted=lambda: self._sent(text), refused=self._unsend, supersedes=True)
        await self._show_and_send(
            (text, "prompt"), PLANNER, SendMessage(agent=PLANNER, text=text), outcome
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
        """Clear the prompt once the planner accepted ``text``, unless the user has edited the
        draft since."""
        prompt = self._parts.prompt
        if prompt.text.strip() == text:
            prompt.clear()

    def _unsend(self) -> None:
        """Undo sending a prompt to the planner, refused or not sent: the planner is freed, the
        prompt's text still in the prompt."""
        self.router.set_busy(PLANNER_LANE, False)

    async def decide(self, proposal: ChecksProposed, decision: DecideProposal) -> None:
        """Note the user's ``decision`` on ``proposal`` in the conversation and send it; refused
        or not sent, the note is dropped and ``proposal`` shown again, unless it was refused as
        no longer waiting."""
        shown = decision_entry(proposal.agent_id, decision)

        def undecide(code: str) -> None:
            if code != UNKNOWN:
                self.proposals.reopen(proposal)

        await self._show_and_send(shown, proposal.agent_id, decision, Awaited(code=undecide))

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
        moved to, marking it read."""
        tabs = self._parts.tabs
        agent = tabs.agent_of(tab_id)
        tabs.mark_read(agent)
        self._parts.conversation.show_only(agent, label)
        self.leave_closed_detail()

    def retitle(self, key: str | None, label: str) -> None:
        """Title the conversation by ``label``, the tab keyed ``key``'s new one, if it still
        shows that tab."""
        conversation = self._parts.conversation
        if key == conversation.only:
            conversation.relabel(label)

    def toggle_planner(self) -> None:
        """Show the planner tab, or go back to the tab shown before it."""
        self._parts.tabs.toggle_planner()

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
            await self.send(RunTasks())

    async def super_review(self) -> None:
        # Reviews have their own lane, so this works while tasks run.
        await self.send(FireTrigger(trigger=SUPER_REVIEW))

    def configure_checks(self) -> None:
        """Ask first, so a stray key doesn't queue a configurator."""
        # The backend queues it first and runs it, or says one is already on its way.
        command = FireTrigger(trigger=CONFIGURE_CHECKS)
        self._app.confirm(
            CONFIGURE_TITLE, CONFIGURE_MESSAGE, CONFIGURE_YES, lambda: self.send(command)
        )

    def reset_planner(self) -> None:
        """Ask first, so a stray key doesn't end the planner's conversation."""
        # The backend refuses while the planner answers, or notes there's nothing to reset.
        command = ResetConversation(agent=PLANNER)
        self._app.confirm(RESET_TITLE, RESET_MESSAGE, RESET_YES, lambda: self.send(command))

    async def cancel_turn(self) -> None:
        # The planner has its own lane, stopped with ctrl+x; ctrl+s stops the task run.
        await self.send(CancelTurn(lane=WORK_LANE))

    async def stop_planner(self) -> None:
        await self.send(CancelTurn(lane=PLANNER_LANE))

    async def cancel_agent(self, agent_id: str) -> None:
        await self.send(CancelAgent(agent_id=agent_id))

    async def accept_task(self, task_id: str) -> None:
        await self.send(AcceptTask(task_id=task_id))

    async def remove_task(self, task_id: str) -> None:
        await self.send(RemoveTask(task_id=task_id))

    async def page_back(self) -> None:
        """Show the page of the session's history before the conversation shown, if any."""
        await self.pager.earlier()

    def edit_task(self, current: TaskState | None) -> None:
        """Open the editor on the task ``current``, or a new one, unless it can't be edited,
        which the user is told; the list changes when the backend reports the new task
        list."""
        refusal = None if current is None else edit_refusal(current)
        if refusal is not None:
            self._app.warn(refusal)
            return

        async def saved(result: TaskSpec | None) -> None:
            if result is None:
                return
            if current is None:
                await self.send(AddTask(task=result))
            else:
                await self.send(EditTask(task_id=current.id, task=result))

        task = None if current is None else current.task
        self._app.edit_task(task, self.router.settings or Settings(), saved)

    def list_sessions(self) -> None:
        """Show the session picker, the session viewed first."""
        self.navigation.place.open()

    def list_workspaces(self) -> None:
        """Show the workspace picker."""
        self.navigation.workspaces.open()

    # The settings pickers, which open once the backend has reported the settings.

    def pick_model(self) -> None:
        if self.router.settings is not None:
            self._parts.pickers.choose_model(self._shown_settings, self._request_settings)

    def pick_tool_access(self) -> None:
        if self.router.settings is not None:
            self._parts.pickers.choose_tools(self._shown_settings, self._request_settings)

    def _shown_settings(self) -> Settings:
        settings = self.router.settings
        assert settings is not None  # Only picked once settings were shown.
        return settings

    async def _request_settings(self, settings: Settings) -> None:
        # The display updates when the backend confirms with SettingsChanged.
        await self.send(UpdateSettings(settings=settings))
