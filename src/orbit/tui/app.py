"""Textual frontend. Talks to the backend only through a protocol Session."""

import time
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass, replace
from typing import ClassVar, assert_never

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widgets import OptionList, Tabs

from orbit import __version__
from orbit.protocol import (
    CONFIGURE_CHECKS,
    INTERNAL,
    PLANNER,
    PLANNER_LANE,
    PROTOCOL_VERSION,
    STORAGE_FAILED,
    SUPER_REVIEW,
    WORK_LANE,
    AcceptTask,
    AddTask,
    BusyChanged,
    BusyLane,
    CancelAgent,
    CancelTurn,
    Catalog,
    ChecksProposed,
    Command,
    CommandRefused,
    DecideProposal,
    EditTask,
    ErrorOccurred,
    Event,
    FireTrigger,
    Hello,
    HistoryPage,
    Link,
    LinkState,
    OwnershipOffered,
    OwnershipTaken,
    RemoveTask,
    ResetConversation,
    ResumeStorage,
    ReviewStatus,
    RunTasks,
    SendMessage,
    Session,
    SessionSummary,
    Settings,
    SettingsChanged,
    Snapshot,
    TakeOwnership,
    TasksChanged,
    TaskSpec,
    TaskState,
    UpdateSettings,
    UsageChanged,
    VerificationChanged,
)
from orbit.tui.agents import AgentsPane
from orbit.tui.commands import CommandDesk
from orbit.tui.conversation import ConversationFeed
from orbit.tui.detail import DetailPane
from orbit.tui.event_stream import event_batches
from orbit.tui.footer import KeyFooter
from orbit.tui.history import HistoryPager
from orbit.tui.link import LinkHost
from orbit.tui.panels import UsagePanel, VerificationPanel
from orbit.tui.proposals import ProposalHost
from orbit.tui.reason import ReasonScreen
from orbit.tui.sessions import SessionsHost
from orbit.tui.storage_halt import StorageHaltHost
from orbit.tui.tabs import ConversationTabs
from orbit.tui.takeover import TakeoverHost
from orbit.tui.task_editor import TaskEditorScreen
from orbit.tui.tasks import TaskList
from orbit.tui.transcript import Transcript
from orbit.tui.transcript_log import TranscriptLog
from orbit.tui.view.confirm import (
    CONFIGURE_MESSAGE,
    CONFIGURE_TITLE,
    CONFIGURE_YES,
    RESET_MESSAGE,
    RESET_TITLE,
    RESET_YES,
)
from orbit.tui.view.events import (
    ANSWER_EVENTS,
    FEED_EVENTS,
    OWNERSHIP_EVENTS,
    SESSION_EVENTS,
    STATE_EVENTS,
    OwnershipEvent,
    ProgressEvent,
    SessionEvent,
    StateEvent,
    coalesce,
    in_group,
)
from orbit.tui.view.log import Entry
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.proposals import decision_entry
from orbit.tui.view.rules import planner_reset_entry
from orbit.tui.view.sessions import chosen_workspace
from orbit.tui.view.settings import (
    model_choices,
    model_options,
    picked_model,
    picked_tool_access,
    tool_access_choices,
    tool_access_labels,
)
from orbit.tui.view.tasks import edit_refusal
from orbit.tui.view.transcript import PLANNER_BUSY
from orbit.tui.view.triggers import trigger_key_labels
from orbit.tui.view.windows import SIDEBAR_WINDOWS, neighbors
from orbit.tui.widgets import (
    ChatPrompt,
    PickerScreen,
    StatusLine,
    confirm,
    mark_selected,
)
from orbit.tui.workspaces import WorkspacesHost


@dataclass(frozen=True)
class Opening:
    """How the app opens: on the workspace at the ``repository`` it runs in, if there's one,
    which a linked app's workspace picker preselects or offers to add, sending ``command``, if
    given, once attached, and with the session picker if ``pick``."""

    command: Command | None = None
    pick: bool = False
    repository: str | None = None


_PLAIN_OPENING = Opening()


# Not inheriting App's bindings drops its ctrl+q quit and ctrl+c "how to quit" hint.
class OrbitApp(App[None], inherit_bindings=False):
    TITLE = "orbit"
    # ctrl+p lists the sessions instead.
    ENABLE_COMMAND_PALETTE = False
    # Start in normal mode: nothing focused; h/j/k/l move the selected window.
    AUTO_FOCUS = None
    CSS = """
    #side { width: 33%; }
    .selected { border: round $accent; }
    ChatPrompt:focus, PromptBox:focus, TaskList:focus, Transcript:focus-within {
        border: heavy $accent;
    }
    DetailPane.selected { border: none; border-top: solid $accent; }
    DetailPane:focus { border-top: heavy $accent; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        # Priority, so it beats the prompt's own ctrl+c (copy).
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
        Binding("h", "move('h')", show=False),
        Binding("j", "move('j')", show=False),
        Binding("k", "move('k')", show=False),
        Binding("l", "move('l')", show=False),
        Binding("enter", "activate", show=False),
        # Below any focused widget's or modal screen's escape; check_action keeps it to
        # normal mode.
        Binding("escape", "release_cursor", show=False),
        # Priority, to beat the screen's focus_next; check_action lets it through
        # to that everywhere but normal mode.
        Binding("tab", "cycle_tab(1)", "Next tab", key_display="Tab", priority=True),
        Binding("shift+tab", "cycle_tab(-1)", show=False, priority=True),
        Binding("i", "insert_mode", "Type"),
        # check_action keeps it to normal mode and the conversation's list mode.
        Binding("t", "toggle_thinking", "Thinking"),
        Binding("ctrl+r", "run_tasks", "Run tasks"),
        Binding("ctrl+g", "super_review", "Super review"),
        Binding("ctrl+e", "configure_checks", "Set up checks"),
        Binding("ctrl+t", "toggle_planner", "Planner"),
        Binding("ctrl+s", "cancel_turn", "Stop run"),
        Binding("ctrl+x", "stop_planner", "Stop planner"),
        Binding("ctrl+n", "reset_planner", "Reset planner"),
        Binding("ctrl+o", "pick_model", "Model"),
        Binding("ctrl+l", "pick_tool_access", "Tools"),
        Binding("ctrl+p", "list_sessions", "Sessions"),
        # check_action keeps it to a control plane in another process, which serves several.
        Binding("ctrl+w", "list_workspaces", "Workspaces"),
        # check_action keeps it from the prompt while typing, which may want ctrl+b itself.
        Binding("ctrl+b", "toggle_sidebar", "Sidebar"),
    ]

    def __init__(
        self,
        session: Session,
        link: Link | None = None,
        opening: Opening = _PLAIN_OPENING,
    ) -> None:
        """The app over ``session``, showing how its ``link`` to a control plane in another
        process stands, if it has one. Over a ``link``, the first catalog opens the workspace
        picker, the workspace at the ``opening``'s repository preselected; otherwise it chooses
        that workspace, or the first. The app attaches to the active session of the workspace
        opened on, then sends the ``opening``'s command, if any; or, if the ``opening`` says to
        pick, opens the session picker, offering to resume the active session only over a
        ``link``, as a session opened in process isn't one picked yet."""
        super().__init__()
        self._session = session
        self._link = link
        self._opening = opening.command
        self._pick = opening.pick
        self._attaching = True  # Until the first catalog has the app attach.
        # Id of the window h/j/k/l last moved to.
        self._selected = "prompt"
        # Which lanes have work in progress, as last reported (or just requested).
        self._busy: dict[BusyLane, bool] = {WORK_LANE: False, PLANNER_LANE: False}
        # Last settings reported by the backend; None until the first report.
        self._settings: Settings | None = None
        self._model_choices: tuple[str, ...] = ()
        self._proposals = ProposalHost(self.push_screen, self._decide)
        repository = opening.repository
        self._sessions = SessionsHost(
            self.push_screen,
            self._send,
            # Over a link the user picks the workspace.
            lambda workspaces: None if link else chosen_workspace(workspaces, repository),
        )
        self._workspaces = WorkspacesHost(
            self.push_screen, self._send, self._open_workspace, repository
        )
        # The feed shows answers once mounted, before any event arrives.
        self._desk = CommandDesk(session.send, self._render_event, lambda e: self._feed.render(e))
        self._takeover = TakeoverHost(
            self.push_screen,
            lambda take, awaited: self._send(TakeOwnership(take=take), awaited),
        )
        # The lines by the prompt, built here so their hosts can show statuses on them.
        self._halt_status = StatusLine(id="storage-halt", classes="error")
        self._link_status = StatusLine(id="link-status")
        self._viewing = StatusLine(id="viewing")
        self._halt = StorageHaltHost(self.push_screen, self._resume_storage, self._halt_status.show)
        self._link_host = LinkHost(
            self.push_screen, self.exit, self._link_status.show, self.set_timer
        )
        # The actions of the keys whose trigger the last catalog lacks, which are hidden.
        self._missing_triggers: frozenset[str] = frozenset()
        self.clock: Callable[[], float] = time.monotonic
        # Pickers are built once and installed so they open instantly.
        self._model_picker = PickerScreen("Model", model_options(())[1])
        self._tool_picker = PickerScreen("Tool access", tool_access_labels())

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical():
                yield ConversationTabs(id="conversation-tabs")
                yield Transcript(id="transcript")
                yield self._halt_status
                yield self._link_status
                yield self._viewing
                yield ChatPrompt(
                    placeholder="Press i to talk to the planner, esc to stop typing", id="prompt"
                )
            with Vertical(id="side"):
                yield TaskList(id="task-list")
                yield VerificationPanel(id="verification-steps")
                yield UsagePanel(id="usage")
                yield AgentsPane(id="agents")
        yield KeyFooter()

    async def on_unmount(self) -> None:
        await self._session.close()

    @property
    def storage_halt(self) -> str | None:
        """Why the session is halted because storage failed; none while it isn't."""
        return self._halt.explanation

    def on_mount(self) -> None:
        # Looked up once: a query walks the DOM, and events arrive many times a second.
        self._transcript = self.query_one(Transcript)
        self._tabs = self.query_one(ConversationTabs)
        self._prompt = self.query_one(ChatPrompt)
        self._task_list = self.query_one(TaskList)
        self._verification = self.query_one(VerificationPanel)
        self._usage = self.query_one(UsagePanel)
        self._agents = self.query_one(AgentsPane)
        self._side = self.query_one("#side")
        # Read through, so a clock set later still times the conversation.
        self._feed = ConversationFeed(
            self._transcript, self._tabs, self._task_list, self._verification, lambda: self.clock()
        )
        self._pager = HistoryPager(self._transcript, self._feed, self._send)
        self.query_one(f"#{self._selected}").add_class("selected")
        self.install_screen(self._model_picker, "pick-model")
        self.install_screen(self._tool_picker, "pick-tool-access")
        if self._link is not None:  # Before the events start, which start the link.
            self._link.watch(self._follow_link)
        self._consume_events()

    @on(ChatPrompt.Submitted, "#prompt")
    async def _submit(self, message: ChatPrompt.Submitted) -> None:
        text = message.text.strip()
        if not text:
            return
        if self._busy[PLANNER_LANE]:
            # Keep the draft for when the planner is free; the user sends it again then.
            self.notify(PLANNER_BUSY, severity="warning")
            return
        self._prompt.action_normal_mode()
        # Busy from now, not from TurnStarted, so a second prompt can't race the turn.
        self._set_busy(PLANNER_LANE, True)
        entry = self._feed.append(text, "prompt", PLANNER)
        outcome = Awaited(accepted=lambda: self._sent(text), refused=lambda: self._unsend(entry))
        await self._send(SendMessage(agent=PLANNER, text=text), outcome)

    def _sent(self, text: str) -> None:
        """Clear the prompt once the planner accepted ``text``, unless the user has edited the
        draft since."""
        if self._prompt.text.strip() == text:
            self._prompt.clear()

    def _unsend(self, entry: Entry) -> None:
        """Undo sending a prompt to the planner, refused or not sent: its prompt ``entry`` is
        dropped and the planner freed, its text still in the prompt."""
        self._set_busy(PLANNER_LANE, False)
        self._transcript.drop_entry(entry)

    # Window navigation only applies at the top level, when no window is active.

    def action_move(self, direction: str) -> None:
        target = self._neighbors().get(direction)
        if target is not None and self.focused is None:
            self._select(target)

    def _neighbors(self) -> dict[str, str]:
        """The windows h/j/k/l reach from the selected one."""
        return neighbors(self._selected, self._transcript.detail.opened, self._side.display)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        conditions: dict[str, Callable[[], bool]] = {
            "cycle_tab": self._normal_mode,
            "release_cursor": self._normal_mode,
            "toggle_thinking": lambda: self._normal_mode() or self._in_transcript(),
            "configure_checks": self._on_main_screen,
            "reset_planner": self._on_main_screen,
            "list_sessions": self._on_main_screen,
            "list_workspaces": lambda: self._link is not None and self._on_main_screen(),
            "toggle_sidebar": lambda: self._on_main_screen() and self.focused is not self._prompt,
        }
        if action in self._missing_triggers:
            return False
        condition = conditions.get(action)
        return condition is None or condition()

    def _on_main_screen(self) -> bool:
        """No modal screen is open."""
        return len(self.screen_stack) == 1

    def _normal_mode(self) -> bool:
        """Nothing focused on the main screen: no typing, list mode or modal screen."""
        return len(self.screen_stack) == 1 and self.focused is None

    def _in_transcript(self) -> bool:
        """The conversation is active, its shown log in list mode, on the main screen."""
        return len(self.screen_stack) == 1 and self.focused is self._transcript.shown

    def action_toggle_thinking(self) -> None:
        """Hide agents' messages while they're still thinking out loud, or show them again."""
        self._transcript.toggle_thinking()

    def action_toggle_sidebar(self) -> None:
        """Hide the sidebar so the conversation takes the full width, or show it again; hiding
        leaves a sidebar window for the conversation."""
        side = self._side
        side.display = not side.display
        if side.display:  # Hidden, the task list had no width to cut its reasons to.
            self.call_after_refresh(self._task_list.fit)
        elif self._selected in SIDEBAR_WINDOWS:
            self.set_focus(None)
            self._select("transcript")

    def action_cycle_tab(self, step: int) -> None:
        tabs = self._tabs
        if step > 0:
            tabs.action_next_tab()
        else:
            tabs.action_previous_tab()

    def action_activate(self) -> None:
        if self._selected == "task-list":
            self._task_list.enter_list_mode()
        elif self._selected == "agents":
            self._agents.enter_list_mode()
        elif self._selected == "transcript":
            self._transcript.enter_list_mode()
        elif self._selected == "detail":
            self._transcript.detail.enter_scroll_mode()
        elif self._selected == "prompt":  # Its viewing mark, while the session is a view.
            self._sessions.offer()

    def action_release_cursor(self) -> None:
        """With the conversation selected, let go of its held cursor; with its detail pane
        selected, close the pane; with the task list selected, let go of it."""
        if self._selected == "transcript":
            self._transcript.release_cursor()
        elif self._selected == "task-list":
            self._task_list.let_go()
        elif self._selected == "detail":
            self._transcript.close_detail()
            self._select("transcript")

    @on(DetailPane.Focused)
    def _detail_focused(self) -> None:
        self._select("detail")

    def action_insert_mode(self) -> None:
        """Shortcut from any window: select the prompt and start typing."""
        if self.focused is None:
            self._select("prompt")
            self._prompt.enter_insert_mode()

    @on(OptionList.OptionSelected, "#task-list")
    def _edit_task(self, event: OptionList.OptionSelected) -> None:
        tasks = self._task_list.tasks
        current = tasks[event.option_index] if event.option_index < len(tasks) else None
        refusal = None if current is None else edit_refusal(current)
        if refusal is not None:
            self.notify(refusal, severity="warning")
            return

        # The list changes when the backend reports the new task list.
        async def saved(result: TaskSpec | None) -> None:
            if result is None:
                return
            if current is None:
                await self._send(AddTask(task=result))
            else:
                await self._send(EditTask(task_id=current.id, task=result))

        editor = TaskEditorScreen(
            None if current is None else current.task,
            self._settings or Settings(),
            lambda: self._model_choices,
            (self._model_picker, self._tool_picker),
        )
        self.push_screen(editor, saved)

    async def action_run_tasks(self) -> None:
        """Ask the backend to run the task list, unless something is already running."""
        if not self._busy[WORK_LANE]:
            await self._send(RunTasks())

    def _select(self, window_id: str) -> None:
        mark_selected(self.query_one(f"#{self._selected}"), False)
        mark_selected(self.query_one(f"#{window_id}"), True)
        self._selected = window_id

    @on(Tabs.TabActivated, "#conversation-tabs")
    def _show_tab(self, event: Tabs.TabActivated) -> None:
        tabs = self._tabs
        agent = tabs.agent_of(event.tab.id or "")
        tabs.mark_read(agent)
        self._transcript.show_only(agent, event.tab.label_text)
        if self._selected == "detail":  # Its pane just closed.
            self._select("transcript")

    @on(ConversationTabs.Relabelled)
    def _retitle_tab(self, message: ConversationTabs.Relabelled) -> None:
        # Another tab may have been shown since the relabel was posted.
        if message.key == self._transcript.only:
            self._transcript.relabel(message.label)

    @on(AgentsPane.Cancel)
    async def _cancel_agent(self, message: AgentsPane.Cancel) -> None:
        await self._send(CancelAgent(agent_id=message.agent_id))

    def action_toggle_planner(self) -> None:
        """Show the planner tab, or go back to the tab shown before it."""
        self._tabs.toggle_planner()

    @on(TaskList.Accept)
    async def _accept_task(self, message: TaskList.Accept) -> None:
        await self._send(AcceptTask(task_id=message.task_id))

    @on(TaskList.Remove)
    async def _remove_task(self, message: TaskList.Remove) -> None:
        await self._send(RemoveTask(task_id=message.task_id))

    @on(TranscriptLog.ReachedTop)
    async def _page_back(self, _: TranscriptLog.ReachedTop) -> None:
        """Show the page of the session's history before the conversation shown, if any."""
        await self._pager.earlier()

    @on(TaskList.ShowReason)
    def _show_reason(self, message: TaskList.ShowReason) -> None:
        self.push_screen(ReasonScreen(message.state, message.number))

    async def action_super_review(self) -> None:
        # Reviews have their own lane, so this works while tasks run.
        await self._send(FireTrigger(trigger=SUPER_REVIEW))

    def action_configure_checks(self) -> None:
        """Ask first, so a stray ctrl+e doesn't queue a configurator."""

        # The backend queues it first and runs it, or says one is already on its way.
        self._confirm_then_send(
            CONFIGURE_TITLE, CONFIGURE_MESSAGE, CONFIGURE_YES, FireTrigger(trigger=CONFIGURE_CHECKS)
        )

    def action_reset_planner(self) -> None:
        """Ask first, so a stray ctrl+n doesn't end the planner's conversation."""

        # The backend refuses while the planner answers, or notes there's nothing to reset.
        self._confirm_then_send(
            RESET_TITLE, RESET_MESSAGE, RESET_YES, ResetConversation(agent=PLANNER)
        )

    def _confirm_then_send(self, title: str, message: str, yes: str, command: Command) -> None:
        """Ask ``title`` and ``message``, sending ``command`` if the user answers ``yes``."""
        confirm(self.push_screen, title, message, yes, lambda: self._send(command))

    def action_list_sessions(self) -> None:
        """Show the session picker, the session viewed first."""
        self._sessions.open()

    def action_list_workspaces(self) -> None:
        """Show the workspace picker."""
        self._workspaces.open()

    async def _open_workspace(self, workspace: str, startup: bool) -> None:
        """Open the session picker of the ``workspace`` picked; at ``startup``, unless the app
        picks a session, attach to its active session and send the command to open with."""
        if startup and not self._pick:
            attach = self._sessions.join(workspace)
            if attach is not None and await self._send(attach):
                await self._send_opening()
            return
        self._sessions.open("resume" if startup else None, workspace)

    async def action_cancel_turn(self) -> None:
        # The planner has its own lane, stopped with ctrl+x; ctrl+s stops the task run.
        await self._send(CancelTurn(lane=WORK_LANE))

    async def action_stop_planner(self) -> None:
        await self._send(CancelTurn(lane=PLANNER_LANE))

    def action_pick_model(self) -> None:
        # While a picker is open, its modal screen blocks the app's key bindings, so a second
        # picker can't be pushed on top.
        if self._settings is not None:
            _, current = model_choices(self._model_choices, self._settings)
            self._model_picker.choose(current, self._request_model)

    def action_pick_tool_access(self) -> None:
        if self._settings is not None:
            _, current = tool_access_choices(self._settings)
            self._tool_picker.choose(current, self._request_tool_access)

    # The settings may have changed while the picker was open, so a pick changes only its own
    # setting of the settings as they are now.
    async def _request_model(self, index: int) -> None:
        assert self._settings is not None  # Only picked once settings were shown.
        await self._request_settings(picked_model(self._model_choices, self._settings, index))

    async def _request_tool_access(self, index: int) -> None:
        assert self._settings is not None  # Only picked once settings were shown.
        await self._request_settings(picked_tool_access(self._settings, index))

    async def _request_settings(self, settings: Settings) -> None:
        # The display updates when the backend confirms with SettingsChanged.
        await self._send(UpdateSettings(settings=settings))

    async def _send(
        self,
        command: Command,
        awaited: Awaited | None = None,
        failure: str = "Couldn't send the command",
    ) -> bool:
        """Whether ``command`` was sent to the open session, as :meth:`CommandDesk.send` sends
        it."""
        return await self._desk.send(command, awaited, failure)

    @work(exclusive=True, group="events")
    async def _consume_events(self) -> None:
        # Closing the batches here, not on garbage collection, stops their reader however this ends.
        async with aclosing(event_batches(self._session.events())) as batches:
            if not await self._hello():
                return
            while batch := await self._next_batch(batches):
                # One repaint for the whole batch.
                with self.batch_update():
                    for event in coalesce(batch):
                        self._render_event(event)
                await self._open_once_listed(batch)

    async def _hello(self) -> bool:
        """Whether the Hello opening the session was sent; a failure to send it is shown."""
        return await self._send(
            Hello(client=f"orbit-tui {__version__}", protocol_version=PROTOCOL_VERSION),
            failure="The backend failed to start the session",
        )

    async def _open_once_listed(self, batch: list[Event]) -> None:
        """Once ``batch`` has the first catalog, open the workspace picker over a link; else
        attach to the active session of the workspace opened on, then send the command to open
        with, if there's one, once that's sent."""
        if not self._first_listed(batch):
            return
        if self._link is not None:
            self._workspaces.open(startup=True)
            return
        attach = self._sessions.opening
        if attach is not None and await self._send(attach):
            await self._send_opening()

    def _first_listed(self, batch: list[Event]) -> bool:
        """Whether ``batch`` has the first catalog, after which the app no longer waits for
        one."""
        if not self._attaching or not any(isinstance(event, Catalog) for event in batch):
            return False
        self._attaching = False
        return True

    async def _send_opening(self) -> None:
        """Send the command to open with, if there's one, only once."""
        if self._opening is not None:
            command, self._opening = self._opening, None
            await self._send(command)

    async def _next_batch(self, batches: AsyncIterator[list[Event]]) -> list[Event]:
        """The next of ``batches``; none once they end, or once reading them fails, which is
        shown."""
        try:
            return await anext(batches, [])
        except Exception as exc:
            self._render_event(
                ErrorOccurred(code=INTERNAL, message=f"The backend's event stream failed: {exc}")
            )
            return []

    def _render_event(self, event: Event) -> None:
        self._follow_agents(event)
        if in_group(event, STATE_EVENTS):
            self._render_state(event)
        elif in_group(event, FEED_EVENTS):
            self._feed.render(event)
        elif in_group(event, SESSION_EVENTS):
            self._render_session(event)
        elif in_group(event, ANSWER_EVENTS):
            self._desk.answer(event)
        else:
            assert_never(event)
        self._follow_halt(event)

    def _render_ownership(self, event: OwnershipEvent) -> None:
        """Ask whether to take over the repository offered, or close the offer once it's
        settled, clearing the read-only mark if this orbit owns it."""
        if isinstance(event, OwnershipOffered):
            self._takeover.offered()
            return
        self._takeover.settled()
        if isinstance(event, OwnershipTaken):
            self._prompt.show_read_only(False)

    def _follow_halt(self, event: Event) -> None:
        """Show the halt a storage error or a command refused for it reports."""
        match event:
            case (
                ErrorOccurred(code=code, message=message)
                | CommandRefused(code=code, message=message)
            ) if code == STORAGE_FAILED:
                self._halt.halted(message)

    async def _resume_storage(self) -> None:
        # Trying again lifts the halt once accepted.
        await self._send(ResumeStorage(), Awaited(accepted=self._halt.lifted))

    def _follow_link(self, state: LinkState) -> None:
        self._link_host.follow(state, self.clock())

    def _render_session(self, event: SessionEvent) -> None:
        """Rule off the planner's conversation where it was reset, show a snapshot of the session
        viewed or a page of its history, follow a catalog, or follow the session's hold on the
        repository."""
        if isinstance(event, Snapshot):
            self._render_snapshot(event)
        elif isinstance(event, Catalog):
            self._render_catalog(event)
        elif isinstance(event, HistoryPage):
            self._pager.show(event)
        elif in_group(event, OWNERSHIP_EVENTS):
            self._render_ownership(event)
        else:
            self._feed.append(*planner_reset_entry(), PLANNER)

    def _render_catalog(self, catalog: Catalog) -> None:
        """Label the trigger keys from ``catalog`` and list its workspaces and sessions, titling
        the conversation with the name of the session viewed, renamed perhaps; one that no
        longer lists the workspace the app is on opens the workspace picker. In process, the
        first catalog opens the session picker, if the app picks one."""
        self._label_triggers(trigger_key_labels(catalog.triggers))
        self._workspaces.follow(catalog)
        self._follow_sessions(catalog)
        attached = self._sessions.attached_session
        if attached is not None:
            self._transcript.show_session(attached.name)
        self._show_viewing()
        if self._pick and self._link is None:
            self._pick = False
            self._sessions.open("pick")

    def _follow_sessions(self, catalog: Catalog) -> None:
        """List ``catalog``'s sessions of the workspace the app is on, letting the user pick
        another once ``catalog`` no longer lists it."""
        was_on = self._sessions.workspace
        self._sessions.follow(catalog)
        if was_on is not None and self._sessions.workspace is None:
            self._workspaces.lost()

    def _label_triggers(self, labels: dict[str, str | None]) -> None:
        """Label each trigger's key in the footer with its label in ``labels``, hiding those
        with none."""
        bindings = self._bindings.key_to_bindings
        missing: set[str] = set()
        for key, label in labels.items():
            binding = bindings[key][0]
            if label is None:
                missing.add(binding.action)
            else:
                bindings[key] = [replace(binding, description=label)]
        self._missing_triggers = frozenset(missing)
        self.refresh_bindings()

    def _open_session(self, session: SessionSummary) -> None:
        """Title the conversation with ``session``'s name, take the task list out of list
        mode, let it follow again and start its total from what ``session`` spent."""
        self._transcript.show_session(session.name)
        if self.focused is self._task_list:
            self.set_focus(None)
        self._task_list.let_go()
        self._task_list.reset_usage(session.usage)

    def _render_snapshot(self, snapshot: Snapshot) -> None:
        """Replace the state shown with ``snapshot``'s, the conversation with its transcript's,
        and open the session it holds, asking about the repository while it says it's offered;
        the agents pane follows it as it follows every event, and the working row once its
        roles are known."""
        self._show_settings(snapshot.settings, snapshot.model_choices)
        for lane, busy in snapshot.busy.items():
            self._set_busy(lane, busy)
        self._show_tasks(snapshot.tasks)
        self._verification.show(snapshot.steps, snapshot.steps_title)
        self._agents.show_review(snapshot.review)
        self._proposals.follow(snapshot.proposals)
        # Its tasks shown first, as the transcript's notes on them read them, and its usage
        # set after, as the transcript's turns count theirs.
        self._feed.replace((envelope.payload for envelope in snapshot.transcript), snapshot.agents)
        self._transcript.track(snapshot, self._tabs.roles)
        self._pager.follow(snapshot)
        self._open_session(snapshot.session)
        self._sessions.view(snapshot)
        self._show_viewing()
        self._prompt.show_read_only(snapshot.read_only)
        self._takeover.follow(snapshot.ownership_offered)
        if snapshot.storage_halt is None:
            self._halt.lifted()
        else:
            self._halt.halted(snapshot.storage_halt)

    def _show_viewing(self) -> None:
        """Mark the prompt while the session viewed isn't the active one, or hide the mark."""
        self._viewing.show(self._sessions.mark)

    def _render_state(self, event: StateEvent) -> None:
        """Show a snapshot of backend state, or a decision it waits for."""
        match event:
            case SettingsChanged(settings=settings, model_choices=choices):
                self._show_settings(settings, choices)
            case BusyChanged(busy=busy, lane=lane):
                self._set_busy(lane, busy)
            case TasksChanged(tasks=tasks):
                self._show_tasks(tasks)
            case _:
                self._show_progress(event)

    def _follow_agents(self, event: Event) -> None:
        self._agents.track(event)
        self._tabs.track(event)
        # A snapshot's roles are known only once it replaces the conversation.
        if not isinstance(event, Snapshot):
            self._transcript.track(event, self._tabs.roles)
        self._proposals.withdraw(event)

    def _show_tasks(self, tasks: tuple[TaskState, ...]) -> None:
        self._task_list.show(tasks)
        self._agents.number_tasks(self._task_list.numbers_by_id)

    def _show_progress(self, event: ProgressEvent) -> None:
        """Show the verification steps' progress, whether review is paused, proposed steps, usage
        limits, or a session's state, which the session picker shows."""
        if isinstance(event, VerificationChanged):
            self._verification.show(event.steps, event.title)
        elif isinstance(event, ReviewStatus):
            self._agents.show_review(event)
        elif isinstance(event, ChecksProposed):
            self._proposals.show(event)
        elif isinstance(event, UsageChanged):
            self._usage.show(event)
        else:
            self._sessions.follow_state(event)
            self._workspaces.follow_state(event)
            self._show_viewing()

    async def _decide(self, proposal: ChecksProposed, decision: DecideProposal) -> None:
        """Note the user's ``decision`` on ``proposal`` in the conversation and send it; refused
        or not sent, the note is dropped and ``proposal`` shown again."""
        entry = self._feed.append(*decision_entry(proposal.agent_id, decision), proposal.agent_id)
        await self._send(decision, Awaited(refused=lambda: self._undecide(proposal, entry)))

    def _undecide(self, proposal: ChecksProposed, entry: Entry) -> None:
        """Undo deciding on ``proposal``: its decision's ``entry`` is dropped and it's shown
        again, to decide on anew."""
        self._transcript.drop_entry(entry)
        self._proposals.reopen(proposal)

    def _show_settings(self, settings: Settings, choices: tuple[str, ...]) -> None:
        if choices != self._model_choices:
            self._model_picker.set_labels(model_options(choices)[1])
        self._settings, self._model_choices = settings, choices
        self._prompt.show_settings(settings)

    def _set_busy(self, lane: BusyLane, busy: bool) -> None:
        self._busy[lane] = busy
        if lane == PLANNER_LANE:
            self._prompt.show_busy(busy)
            self._tabs.set_planner_busy(busy)
