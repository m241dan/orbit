"""Textual frontend. Talks to the backend only through a protocol Session."""

import asyncio
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import aclosing, suppress
from dataclasses import replace
from typing import ClassVar, assert_never
from uuid import uuid4

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
    SUPER_REVIEW,
    WORK_LANE,
    AcceptTask,
    AddTask,
    AgentExited,
    AgentReply,
    AgentRequest,
    AgentStarted,
    AssistantMessage,
    BusyChanged,
    BusyLane,
    CancelAgent,
    CancelTurn,
    Catalog,
    ChecksProposed,
    Command,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    ConversationReset,
    DecideProposal,
    EditTask,
    ErrorOccurred,
    Event,
    EventEnvelope,
    FireTrigger,
    Hello,
    ListSessions,
    Notice,
    ProgressNoted,
    RemoveTask,
    ResetConversation,
    ReviewStatus,
    RunTasks,
    SendMessage,
    Session,
    SessionsListed,
    SessionSummary,
    Settings,
    SettingsChanged,
    Snapshot,
    StartupChecked,
    TaskGaveUp,
    TaskMerged,
    TasksChanged,
    TaskSpec,
    TaskStarted,
    TaskState,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
    UpdateSettings,
    UsageChanged,
    VerificationChanged,
    VerificationFinished,
    VerificationSkipped,
    VerificationStarted,
    Welcome,
)
from orbit.tui.agents import AgentsPane
from orbit.tui.conversation import ConversationFeed
from orbit.tui.detail import DetailPane
from orbit.tui.panels import UsagePanel, VerificationPanel
from orbit.tui.proposals import ProposalHost
from orbit.tui.sessions import SessionsHost
from orbit.tui.tabs import ConversationTabs
from orbit.tui.tasks import ReasonScreen, TaskEditorScreen, TaskList
from orbit.tui.transcript import Transcript
from orbit.tui.view import (
    CONFIGURE_MESSAGE,
    CONFIGURE_TITLE,
    CONFIGURE_YES,
    PLANNER_BUSY,
    RESET_MESSAGE,
    RESET_TITLE,
    RESET_YES,
    SIDEBAR_WINDOWS,
    coalesce,
    decision_entry,
    edit_refusal,
    model_choices,
    model_options,
    neighbors,
    planner_reset_entry,
    tool_access_choices,
    tool_access_labels,
    trigger_key_labels,
)
from orbit.tui.widgets import (
    ChatPrompt,
    ConfirmScreen,
    KeyFooter,
    PickerScreen,
    mark_selected,
)

# Most events rendered in one go, so a flood still lets the screen repaint now and then.
BATCH_LIMIT = 500


async def event_batches(
    events: AsyncIterator[EventEnvelope], limit: int = BATCH_LIMIT
) -> AsyncGenerator[list[Event]]:
    """Batches of the events in ``events``' envelopes: each the next event and those already
    waiting behind it, up to ``limit``. Ends when ``events`` do, raising what reading them
    raised."""
    queue: asyncio.Queue[Event | None] = asyncio.Queue()

    async def read() -> None:
        try:
            async for envelope in events:
                queue.put_nowait(envelope.payload)
        finally:
            queue.put_nowait(None)  # The end.

    reader = asyncio.create_task(read())
    try:
        while batch := await _next_batch(queue, limit):
            yield batch
    finally:
        reader.cancel()
        with suppress(asyncio.CancelledError):
            await reader


async def _next_batch(queue: asyncio.Queue[Event | None], limit: int) -> list[Event]:
    """The next event in ``queue`` and those waiting behind it, up to ``limit``; none at the
    end, which stays queued for the next call."""
    batch: list[Event] = []
    event = await queue.get()
    while event is not None:
        batch.append(event)
        if len(batch) == limit or queue.empty():
            return batch
        event = queue.get_nowait()
    queue.put_nowait(None)
    return batch


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
        # check_action keeps it from the prompt while typing, which may want ctrl+b itself.
        Binding("ctrl+b", "toggle_sidebar", "Sidebar"),
    ]

    def __init__(self, session: Session) -> None:
        super().__init__()
        self._session = session
        # Id of the window h/j/k/l last moved to.
        self._selected = "prompt"
        # Which lanes have work in progress, as last reported (or just requested).
        self._busy: dict[BusyLane, bool] = {WORK_LANE: False, PLANNER_LANE: False}
        # Last settings reported by the backend; None until the first report.
        self._settings: Settings | None = None
        self._model_choices: tuple[str, ...] = ()
        self._proposals = ProposalHost(self.push_screen, self._decide)
        self._sessions = SessionsHost(self.push_screen, self._send)
        self._message_id: str | None = None  # The command id of the last message sent.
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
        self.query_one(f"#{self._selected}").add_class("selected")
        self.install_screen(self._model_picker, "pick-model")
        self.install_screen(self._tool_picker, "pick-tool-access")
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
        message.prompt.clear()
        prompt = self._prompt
        prompt.action_normal_mode()
        # Busy from now, not from TurnStarted, so a second prompt can't race the turn.
        self._set_busy(PLANNER_LANE, True)
        self._feed.append(text, "prompt", PLANNER)
        await self._send(SendMessage(agent=PLANNER, text=text))

    def _render_outcome(self, event: CommandAccepted | CommandRefused | Welcome) -> None:
        """Show why a command was refused, freeing the planner from a message it refused; an
        accepted one, or the Welcome answering Hello, shows nothing."""
        if isinstance(event, CommandAccepted | Welcome):
            return
        if event.command_id == self._message_id:
            self._set_busy(PLANNER_LANE, False)
        self._feed.render(event)

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

    @on(TaskList.ShowReason)
    def _show_reason(self, message: TaskList.ShowReason) -> None:
        self.push_screen(ReasonScreen(message.state, message.number))

    async def action_super_review(self) -> None:
        # Reviews have their own lane, so this works while tasks run.
        await self._send(FireTrigger(trigger=SUPER_REVIEW))

    def action_configure_checks(self) -> None:
        """Ask first, so a stray ctrl+e doesn't queue a configurator."""

        async def answered(confirmed: bool | None) -> None:
            # The backend queues it first and runs it, or says one is already on its way.
            if confirmed:
                await self._send(FireTrigger(trigger=CONFIGURE_CHECKS))

        self.push_screen(ConfirmScreen(CONFIGURE_TITLE, CONFIGURE_MESSAGE, CONFIGURE_YES), answered)

    def action_reset_planner(self) -> None:
        """Ask first, so a stray ctrl+n doesn't end the planner's conversation."""

        async def answered(confirmed: bool | None) -> None:
            # The backend refuses while the planner answers, or notes there's nothing to reset.
            if confirmed:
                await self._send(ResetConversation(agent=PLANNER))

        self.push_screen(ConfirmScreen(RESET_TITLE, RESET_MESSAGE, RESET_YES), answered)

    async def action_list_sessions(self) -> None:
        """Ask for the saved sessions, which show in a modal once they're listed."""
        self._sessions.asked()
        await self._send(ListSessions())

    def _name_current(self, listed: SessionsListed) -> None:
        """Title the conversation with the name of the open session among those ``listed``,
        renamed perhaps; a listing without it leaves the title as it is."""
        current = [summary for summary in listed.sessions if summary.current]
        if current:
            self._transcript.show_session(current[0].name)

    async def action_cancel_turn(self) -> None:
        # The planner has its own lane, stopped with ctrl+x; ctrl+s stops the task run.
        await self._send(CancelTurn(lane=WORK_LANE))

    async def action_stop_planner(self) -> None:
        await self._send(CancelTurn(lane=PLANNER_LANE))

    def action_pick_model(self) -> None:
        # While a picker is open, its modal screen blocks the app's key bindings, so a second
        # picker can't be pushed on top.
        if self._settings is not None:
            options, current = model_choices(self._model_choices, self._settings)
            self._model_picker.choose(options, current, self._request_settings)

    def action_pick_tool_access(self) -> None:
        if self._settings is not None:
            options, current = tool_access_choices(self._settings)
            self._tool_picker.choose(options, current, self._request_settings)

    async def _request_settings(self, settings: Settings) -> None:
        # The display updates when the backend confirms with SettingsChanged.
        await self._send(UpdateSettings(settings=settings))

    async def _send(self, command: Command, failure: str = "Couldn't send the command") -> bool:
        """Whether ``command`` was sent to the open session in the default workspace, under a
        fresh id remembered for a message; a failure to send it is shown after ``failure``,
        freeing the planner from a message, as a refusal does."""
        command_id = uuid4().hex
        if isinstance(command, SendMessage):
            self._message_id = command_id
        try:
            await self._session.send(CommandEnvelope(command_id=command_id, payload=command))
        except Exception as exc:
            if isinstance(command, SendMessage):
                self._set_busy(PLANNER_LANE, False)
            self._render_event(ErrorOccurred(code=INTERNAL, message=f"{failure}: {exc}"))
            return False
        return True

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

    async def _hello(self) -> bool:
        """Whether the Hello opening the session was sent; a failure to send it is shown."""
        return await self._send(
            Hello(client=f"orbit-tui {__version__}", protocol_version=PROTOCOL_VERSION),
            "The backend failed to start the session",
        )

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
        match event:
            case (
                SettingsChanged()
                | BusyChanged()
                | TasksChanged()
                | VerificationChanged()
                | ReviewStatus()
                | ChecksProposed()
                | UsageChanged()
            ):
                self._render_state(event)
            case (
                TurnStarted()
                | TurnFinished()
                | ErrorOccurred()
                | AssistantMessage()
                | ToolCallStarted()
                | ToolCallFinished()
                | TaskStarted()
                | VerificationStarted()
                | VerificationSkipped()
                | VerificationFinished()
                | TaskMerged()
                | AgentStarted()
                | TaskGaveUp()
                | AgentExited()
                | AgentRequest()
                | AgentReply()
                | ProgressNoted()
                | Notice()
                | StartupChecked()
            ):
                self._feed.render(event)
            case SessionsListed() | ConversationReset() | Snapshot() | Catalog():
                self._render_session(event)
            case CommandAccepted() | CommandRefused() | Welcome():
                self._render_outcome(event)
            case _:
                assert_never(event)

    def _render_session(
        self, event: SessionsListed | ConversationReset | Snapshot | Catalog
    ) -> None:
        """Show the sessions listed, rule off the planner's conversation where it was reset,
        show a snapshot of the session open, or label the trigger keys from a catalog. The
        conversation's title follows the open session's name."""
        if isinstance(event, SessionsListed):
            self._name_current(event)
            self._sessions.show(event)
        elif isinstance(event, Snapshot):
            self._render_snapshot(event)
        elif isinstance(event, Catalog):
            self._label_triggers(trigger_key_labels(event.triggers))
        else:
            self._feed.append(*planner_reset_entry(), PLANNER)

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
        """Replace the state shown, but for the transcript, with ``snapshot``'s, and open the
        session it holds; the agents pane follows it as it follows every event."""
        self._show_settings(snapshot.settings, snapshot.model_choices)
        for lane, busy in snapshot.busy.items():
            self._set_busy(lane, busy)
        self._show_tasks(snapshot.tasks)
        self._verification.show(snapshot.steps, snapshot.steps_title)
        self._agents.show_review(snapshot.review)
        self._proposals.follow(snapshot.proposals)
        self._open_session(snapshot.session)

    def _render_state(
        self,
        event: SettingsChanged
        | BusyChanged
        | TasksChanged
        | VerificationChanged
        | ReviewStatus
        | ChecksProposed
        | UsageChanged,
    ) -> None:
        """Show a snapshot of backend state, or a decision it waits for."""
        match event:
            case SettingsChanged(settings=settings, model_choices=choices):
                self._show_settings(settings, choices)
            case BusyChanged(busy=busy, lane=lane):
                self._set_busy(lane, busy)
            case TasksChanged(tasks=tasks):
                self._show_tasks(tasks)
            case VerificationChanged() | ReviewStatus() | ChecksProposed() | UsageChanged():
                self._show_progress(event)
            case _:
                assert_never(event)

    def _follow_agents(self, event: Event) -> None:
        self._agents.track(event)
        self._tabs.track(event)
        self._transcript.track(event, self._tabs.roles)
        self._proposals.withdraw(event)

    def _show_tasks(self, tasks: tuple[TaskState, ...]) -> None:
        self._task_list.show(tasks)
        self._agents.number_tasks(self._task_list.numbers_by_id)

    def _show_progress(
        self, event: VerificationChanged | ReviewStatus | ChecksProposed | UsageChanged
    ) -> None:
        """Show the verification steps' progress, whether review is paused, proposed steps, or
        usage limits."""
        if isinstance(event, VerificationChanged):
            self._verification.show(event.steps, event.title)
        elif isinstance(event, ReviewStatus):
            self._agents.show_review(event)
        elif isinstance(event, ChecksProposed):
            self._proposals.show(event)
        else:
            self._usage.show(event)

    async def _decide(self, proposal: ChecksProposed, decision: DecideProposal) -> None:
        """Note the user's ``decision`` on ``proposal`` in the conversation and send it."""
        self._feed.append(*decision_entry(proposal.agent_id, decision), proposal.agent_id)
        await self._send(decision)

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
