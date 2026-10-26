"""Textual frontend. Talks to the backend only through a protocol Session."""

import time
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import replace
from typing import ClassVar

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widgets import Tabs

from orbit.protocol import Command, Connected, Link, LinkState, Session, TaskSpec
from orbit.tracing import NoTelemetry, Telemetry
from orbit.tui.agents import AgentsPane
from orbit.tui.approvals import ApprovalHost
from orbit.tui.detail import DetailPane
from orbit.tui.event_stream import event_batches
from orbit.tui.footer import KeyFooter
from orbit.tui.link import LinkHost
from orbit.tui.panels import UsagePanel, VerificationPanel
from orbit.tui.proposals import ProposalHost
from orbit.tui.reason import ReasonScreen
from orbit.tui.reports import ReportsHost
from orbit.tui.sessions import SessionsHost
from orbit.tui.settings_pickers import SettingsPickers
from orbit.tui.storage_halt import StorageHaltHost
from orbit.tui.tabs import ConversationTabs
from orbit.tui.takeover import TakeoverHost
from orbit.tui.task_editor import TaskEditorScreen
from orbit.tui.tasks import TaskList
from orbit.tui.transcript import Transcript
from orbit.tui.transcript_log import TranscriptLog
from orbit.tui.view.actions import carrying
from orbit.tui.view.frontend import Frontend, Parts, Saved
from orbit.tui.view.opening import Opening
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.seats import PLANNER_SEAT
from orbit.tui.view.spend import SPENDING_HINT, SPENDING_TITLE
from orbit.tui.view.task_editor import TaskChoices
from orbit.tui.view.transcript import prompt_placeholder
from orbit.tui.warning_capture import WarningCapture
from orbit.tui.widgets import ChatPrompt, StatusLine, TextScreen, confirm, mark_selected
from orbit.tui.workspaces import WorkspacesHost

_PLAIN_OPENING = Opening()
_NO_TELEMETRY = NoTelemetry()


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
        Binding("ctrl+t", "cycle_conversations", "Conversations"),
        Binding("ctrl+s", "cancel_turn", "Stop run"),
        Binding("ctrl+x", "stop_planner", "Stop turn"),
        Binding("ctrl+n", "reset_planner", "Reset conversation"),
        Binding("ctrl+o", "pick_model", "Model"),
        Binding("ctrl+l", "pick_tool_access", "Tools"),
        # While typing, the prompt's own ctrl+y (redo) comes first.
        Binding("ctrl+y", "pick_effort", "Effort"),
        Binding("ctrl+p", "list_sessions", "Sessions"),
        # check_action keeps it to a control plane in another process, which serves several.
        Binding("ctrl+w", "list_workspaces", "Workspaces"),
        Binding("ctrl+k", "list_reports", "Reports"),
        # check_action keeps it to normal mode; the frontend to a conversational role's tab.
        Binding("u", "show_spending", show=False),
        # check_action keeps it from the prompt while typing, which may want ctrl+b itself.
        Binding("ctrl+b", "toggle_sidebar", "Sidebar"),
    ]

    def __init__(
        self,
        session: Session,
        link: Link | None = None,
        opening: Opening = _PLAIN_OPENING,
        telemetry: Telemetry = _NO_TELEMETRY,
    ) -> None:
        """The app over ``session``, showing how its ``link`` to a control plane in another
        process stands, if it has one. Over a ``link``, the first catalog opens the workspace
        picker, the workspace at the ``opening``'s repository preselected; otherwise it chooses
        that workspace, or the first. The app attaches to the active session of the workspace
        opened on, then sends the ``opening``'s command, if any; or, if the ``opening`` says to
        pick, opens the session picker, offering to resume the active session only over a
        ``link``, as a session opened in process isn't one picked yet. Each user action sending
        commands is traced with ``telemetry``, its commands carrying its span's context."""
        super().__init__()
        self._session = session
        self._link = link
        self._opening = opening
        self._telemetry = telemetry
        self._proposals = ProposalHost(self.push_screen)
        self._approvals = ApprovalHost(self.push_screen)
        self._sessions = SessionsHost(
            self.push_screen, self._send, lambda: self._frontend.router.show_viewing()
        )
        self._workspaces = WorkspacesHost(self.push_screen, self._send)
        self._takeover = TakeoverHost(self.push_screen, self._send).offer
        self._reports = ReportsHost(self.push_screen, self._send)
        # The lines by the prompt, built here so their hosts can show statuses on them.
        self._halt_status = StatusLine(id="storage-halt", classes="error")
        self._link_status = StatusLine(id="link-status")
        self._viewing = StatusLine(id="viewing")
        self._paused = StatusLine(id="paused")
        self._halt = StorageHaltHost(self.push_screen, self._send, self._halt_status.show).halt
        self._link_host = LinkHost(
            self.push_screen, self.exit, self._link_status.show, self.set_timer
        )
        # The actions of the keys whose trigger the last catalog lacks, which are hidden.
        self._missing_triggers: frozenset[str] = frozenset()
        self.clock: Callable[[], float] = time.monotonic
        self._pickers = SettingsPickers()
        # Orbit's and OpenTelemetry's warnings, shown as notes while the app runs.
        self.warnings = WarningCapture()

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical():
                yield ConversationTabs(id="conversation-tabs")
                yield Transcript(id="transcript")
                yield self._halt_status
                yield self._link_status
                yield self._viewing
                yield self._paused
                yield ChatPrompt(placeholder=prompt_placeholder(PLANNER_SEAT.label), id="prompt")
            with Vertical(id="side"):
                yield TaskList(id="task-list")
                yield VerificationPanel(id="verification-steps")
                yield UsagePanel(id="usage")
                yield AgentsPane(id="agents")
        yield KeyFooter()

    async def on_unmount(self) -> None:
        self.warnings.detach()
        await self._session.close()

    @property
    def storage_halt(self) -> str | None:
        """Why the session is halted because storage failed; none while it isn't."""
        return self._frontend.storage_halt

    def on_mount(self) -> None:
        # Looked up once: a query walks the DOM, and events arrive many times a second.
        self._transcript = self.query_one(Transcript)
        self._prompt = self.query_one(ChatPrompt)
        self._task_list = self.query_one(TaskList)
        self._agents = self.query_one(AgentsPane)
        self._side = self.query_one("#side")
        send = carrying(self._session.send, self._telemetry)
        self._frontend = Frontend(self._parts(), send, self._opening, self._link is not None)
        self._frontend.start()
        self._pickers.install(self)
        if self._link is not None:  # Before the events start, which start the link.
            self._link.watch(self._follow_link)
        self.warnings.attach(self._frontend.note_warning)
        self._consume_events()

    def _parts(self) -> Parts:
        """The widgets and hosts the frontend shows in, their models where they have them."""
        transcript, prompt = self._transcript, self._prompt
        return Parts(
            app=self,
            conversation=transcript.conversation,
            paging=transcript,
            tabs=self.query_one(ConversationTabs).strip,
            prompt=prompt,
            marks=prompt.marks,
            viewing=self._viewing,
            paused=self._paused,
            tasks=self._task_list.board,
            steps=self.query_one(VerificationPanel).verification,
            usage=self.query_one(UsagePanel),
            agents=self._agents.agents,
            place=self._sessions,
            workspaces=self._workspaces,
            proposals=self._proposals,
            approvals=self._approvals,
            pickers=self._pickers.model,
            halt=self._halt,
            takeover=self._takeover,
            reports=self._reports.shelf,
            # Read through, so a clock set later still times the conversation.
            clock=lambda: self.clock(),
        )

    # What the frontend asks of the app itself.

    @property
    def nothing_focused(self) -> bool:
        return self.focused is None

    @property
    def detail_open(self) -> bool:
        return self._transcript.detail.opened

    @property
    def sidebar_shown(self) -> bool:
        return self._side.display

    def warn(self, message: str) -> None:
        self.notify(message, severity="warning")

    def confirm(
        self, title: str, message: str, yes: str, then: Callable[[], Awaitable[object]]
    ) -> None:
        confirm(self.push_screen, title, message, yes, then)

    def edit_task(self, task: TaskSpec | None, choices: TaskChoices, saved: Saved) -> None:
        self.push_screen(TaskEditorScreen(task, choices, self._pickers.model), saved)

    def show_spending(self, text: str) -> None:
        self.push_screen(TextScreen(SPENDING_TITLE, SPENDING_HINT, text))

    def label_triggers(self, labels: dict[str, str | None]) -> None:
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

    def mark_selected(self, window: str, selected: bool) -> None:
        mark_selected(self.query_one(f"#{window}"), selected)

    def enter(self, window: str) -> None:
        if window == "task-list":
            self._task_list.enter_list_mode()
        elif window == "agents":
            self._agents.enter_list_mode()
        elif window == "transcript":
            self._transcript.enter_list_mode()
        elif window == "detail":
            self._transcript.detail.enter_scroll_mode()

    def unfocus(self) -> None:
        self.set_focus(None)

    def unfocus_task_list(self) -> None:
        """Take the focus from the task list, if it has it."""
        if self.focused is self._task_list:
            self.set_focus(None)

    # The keys and widgets' messages, as the frontend takes them.

    @on(ChatPrompt.Submitted, "#prompt")
    async def _submit(self, message: ChatPrompt.Submitted) -> None:
        await self._frontend.submit(message.text)

    # Window navigation only applies at the top level, when no window is active.

    def action_move(self, direction: str) -> None:
        self._frontend.move(direction)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        conditions: dict[str, Callable[[], bool]] = {
            "cycle_tab": self._normal_mode,
            "show_spending": self._normal_mode,
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
        self._frontend.toggle_thinking()

    def action_toggle_sidebar(self) -> None:
        """Hide the sidebar so the conversation takes the full width, or show it again; hiding
        leaves a sidebar window for the conversation."""
        side = self._side
        side.display = not side.display
        if side.display:  # Hidden, the task list had no width to cut its reasons to.
            self.call_after_refresh(self._task_list.fit)
        self._frontend.sidebar_toggled(side.display)

    def action_show_spending(self) -> None:
        """Show what each role spent itself and caused its consultants to spend."""
        self._frontend.show_spending()

    def action_cycle_tab(self, step: int) -> None:
        self._frontend.cycle_tab(step)

    def action_activate(self) -> None:
        self._frontend.activate()

    def action_release_cursor(self) -> None:
        self._frontend.release_cursor()

    @on(DetailPane.Focused)
    def _detail_focused(self) -> None:
        self._frontend.detail_focused()

    def action_insert_mode(self) -> None:
        """Shortcut from any window: select the prompt and start typing."""
        self._frontend.insert_mode()

    @on(TaskList.Edit)
    def _edit_task(self, message: TaskList.Edit) -> None:
        self._frontend.edit_task(message.task)

    async def action_run_tasks(self) -> None:
        await self._frontend.run_tasks()

    @on(Tabs.TabActivated, "#conversation-tabs")
    def _show_tab(self, event: Tabs.TabActivated) -> None:
        self._frontend.tab_shown(event.tab.id or "", event.tab.label_text)

    @on(ConversationTabs.Relabelled)
    def _retitle_tab(self, message: ConversationTabs.Relabelled) -> None:
        # Another tab may have been shown since the relabel was posted.
        self._frontend.retitle(message.key, message.label)

    @on(AgentsPane.Cancel)
    async def _cancel_agent(self, message: AgentsPane.Cancel) -> None:
        await self._frontend.cancel_agent(message.agent_id)

    def action_cycle_conversations(self) -> None:
        """Show the planner tab, from there each other conversational role's, and from the
        last, the tab shown before."""
        self._frontend.cycle_conversations()

    @on(TaskList.Accept)
    async def _accept_task(self, message: TaskList.Accept) -> None:
        await self._frontend.accept_task(message.task_id)

    @on(TaskList.Remove)
    async def _remove_task(self, message: TaskList.Remove) -> None:
        await self._frontend.remove_task(message.task_id)

    @on(TranscriptLog.ReachedTop)
    async def _page_back(self, _: TranscriptLog.ReachedTop) -> None:
        await self._frontend.page_back()

    @on(TaskList.ShowReason)
    def _show_reason(self, message: TaskList.ShowReason) -> None:
        self.push_screen(ReasonScreen(message.state, message.number))

    async def action_super_review(self) -> None:
        await self._frontend.super_review()

    def action_configure_checks(self) -> None:
        self._frontend.configure_checks()

    def action_reset_planner(self) -> None:
        self._frontend.reset_planner()

    def action_list_sessions(self) -> None:
        """Show the session picker, the session viewed first."""
        self._frontend.list_sessions()

    def action_list_workspaces(self) -> None:
        """Show the workspace picker."""
        self._frontend.list_workspaces()

    async def action_list_reports(self) -> None:
        """Show the workspace's reports."""
        await self._frontend.list_reports()

    async def action_cancel_turn(self) -> None:
        await self._frontend.cancel_turn()

    async def action_stop_planner(self) -> None:
        await self._frontend.stop_planner()

    def action_pick_model(self) -> None:
        # While a picker is open, its modal screen blocks the app's key bindings, so a second
        # picker can't be pushed on top.
        self._frontend.pick_model()

    def action_pick_tool_access(self) -> None:
        self._frontend.pick_tool_access()

    def action_pick_effort(self) -> None:
        self._frontend.pick_effort()

    async def _send(
        self,
        command: Command,
        awaited: Awaited | None = None,
        failure: str = "Couldn't send the command",
    ) -> bool:
        """Whether ``command`` was sent to the open session, as the frontend sends it."""
        return await self._frontend.send(command, awaited, failure)

    @work(exclusive=True, group="events")
    async def _consume_events(self) -> None:
        frontend = self._frontend
        # Closing the batches here, not on garbage collection, stops their reader however this ends.
        async with aclosing(event_batches(self._session.events())) as batches:
            if not await frontend.hello():
                return
            while batch := await frontend.next_batch(batches):
                # One repaint for the whole batch.
                with self.batch_update():
                    frontend.show_batch(batch)
                await frontend.listed(batch)

    def _follow_link(self, state: LinkState) -> None:
        self._link_host.monitor.follow(state, self.clock())
        if isinstance(state, Connected):
            self._frontend.navigation.reconnected()
