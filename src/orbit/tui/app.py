"""Textual frontend. Talks to the backend only through a protocol Session."""

from typing import ClassVar, assert_never

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Input, OptionList, Tabs

from orbit.protocol import (
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
    ChecksProposed,
    ConfigureChecks,
    DecideProposal,
    EditTask,
    ErrorOccurred,
    Event,
    Notice,
    ProgressNoted,
    RemoveTask,
    RequestSuperReview,
    ReviewStatus,
    RunTasks,
    Session,
    Settings,
    SettingsChanged,
    StartupChecked,
    StepState,
    SubmitPrompt,
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
)
from orbit.tui.agents import AgentsPane
from orbit.tui.proposals import ProposalScreen
from orbit.tui.tasks import TaskEditorScreen, TaskList
from orbit.tui.view import (
    PLANNER,
    TOOL_ACCESS_DESCRIPTIONS,
    TOOL_ACCESS_OPTIONS,
    EntryKind,
    activity_entry,
    agent_labelled,
    decision_entry,
    note_entry,
    output_entry,
    task_started_entry,
    task_tab_key,
    turn_end_entry,
    verification_finished_entry,
    verification_skipped_entry,
    verification_started_entry,
)
from orbit.tui.widgets import (
    CONFIGURE_MESSAGE,
    CONFIGURE_TITLE,
    CONFIGURE_YES,
    ConfirmScreen,
    ConversationTabs,
    PickerScreen,
    PromptInput,
    Transcript,
    UsagePanel,
    VerificationPanel,
)

# Window reached from each window by h/j/k/l; missing means an edge.
# Conversation over prompt on the left, task list over verification steps on the right.
NEIGHBORS: dict[str, dict[str, str]] = {
    "transcript": {"j": "prompt", "l": "task-list"},
    "prompt": {"k": "transcript", "l": "verification-steps"},
    "task-list": {"h": "transcript", "j": "verification-steps"},
    "verification-steps": {"h": "prompt", "k": "task-list", "j": "agents"},
    "agents": {"h": "prompt", "k": "verification-steps"},
}


# Not inheriting App's bindings drops its ctrl+q quit and ctrl+c "how to quit" hint.
class OrbitApp(App[None], inherit_bindings=False):
    TITLE = "orbit"
    # Start in normal mode: nothing focused; h/j/k/l move the selected window.
    AUTO_FOCUS = None
    CSS = """
    #side { width: 33%; }
    .selected { border: round $accent; }
    PromptInput:focus, PromptBox:focus, TaskList:focus, Transcript:focus-within {
        border: heavy $accent;
    }
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
        Binding("ctrl+r", "run_tasks", "Run tasks"),
        Binding("ctrl+g", "super_review", "Super review"),
        Binding("ctrl+e", "configure_checks", "Set up checks"),
        Binding("ctrl+t", "toggle_planner", "Planner"),
        Binding("ctrl+s", "cancel_turn", "Stop run"),
        Binding("ctrl+x", "stop_planner", "Stop planner"),
        Binding("ctrl+o", "pick_model", "Model"),
        Binding("ctrl+l", "pick_tool_access", "Tools"),
    ]

    def __init__(self, session: Session) -> None:
        super().__init__()
        self._session = session
        # Id of the window h/j/k/l last moved to.
        self._selected = "prompt"
        # Which lanes have work in progress, as last reported (or just requested).
        self._busy: dict[BusyLane, bool] = {"work": False, "planner": False}
        # Last settings reported by the backend; None until the first report.
        self._settings: Settings | None = None
        self._model_choices: tuple[str, ...] = ()
        self._proposal: ProposalScreen | None = None  # The proposal being shown, if any.
        # Pickers are built once and installed so they open instantly.
        self._model_picker = PickerScreen("Model", ["default"])
        self._tool_picker = PickerScreen(
            "Tool access", [TOOL_ACCESS_DESCRIPTIONS[a] for a in TOOL_ACCESS_OPTIONS]
        )

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical():
                yield ConversationTabs(id="conversation-tabs")
                yield Transcript(id="transcript")
                yield PromptInput(
                    placeholder="Press i to talk to the planner, esc to stop typing", id="prompt"
                )
            with Vertical(id="side"):
                yield TaskList(id="task-list")
                yield VerificationPanel(id="verification-steps")
                yield UsagePanel(id="usage")
                yield AgentsPane(id="agents")
        yield Footer()

    async def on_unmount(self) -> None:
        await self._session.close()

    def on_mount(self) -> None:
        # Looked up once: a query walks the DOM, and events arrive many times a second.
        self._transcript = self.query_one(Transcript)
        self._tabs = self.query_one(ConversationTabs)
        self._prompt = self.query_one(PromptInput)
        self._task_list = self.query_one(TaskList)
        self._verification = self.query_one(VerificationPanel)
        self._usage = self.query_one(UsagePanel)
        self._agents = self.query_one(AgentsPane)
        self.query_one(f"#{self._selected}").add_class("selected")
        self.install_screen(self._model_picker, "pick-model")
        self.install_screen(self._tool_picker, "pick-tool-access")
        self._consume_events()

    @on(Input.Submitted, "#prompt")
    async def _submit(self, message: Input.Submitted) -> None:
        text = message.value.strip()
        if not text:
            return
        message.input.clear()
        prompt = self._prompt
        prompt.action_normal_mode()
        # Busy from now, not from TurnStarted, so a second prompt can't race the turn.
        self._set_busy("planner", True)
        self._append(text, "user", PLANNER)
        await self._session.send(SubmitPrompt(text=text))

    # Window navigation only applies at the top level, when no window is active.

    def action_move(self, direction: str) -> None:
        target = NEIGHBORS[self._selected].get(direction)
        if target is not None and self.focused is None:
            self._select(target)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("cycle_tab", "release_cursor"):
            return self._normal_mode()
        if action == "configure_checks":
            return len(self.screen_stack) == 1
        return True

    def _normal_mode(self) -> bool:
        """Nothing focused on the main screen: no typing, list mode or modal screen."""
        return len(self.screen_stack) == 1 and self.focused is None

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

    def action_release_cursor(self) -> None:
        """With the conversation selected, let go of its held cursor."""
        if self._selected == "transcript":
            self._transcript.release_cursor()

    def action_insert_mode(self) -> None:
        """Shortcut from any window: select the prompt and start typing."""
        if self.focused is None:
            self._select("prompt")
            self._prompt.enter_insert_mode()

    @on(OptionList.OptionSelected, "#task-list")
    def _edit_task(self, event: OptionList.OptionSelected) -> None:
        tasks = self._task_list.tasks
        current = tasks[event.option_index] if event.option_index < len(tasks) else None

        # The list changes when the backend reports the new task list.
        async def saved(result: TaskSpec | None) -> None:
            if result is None:
                return
            if current is None:
                await self._session.send(AddTask(task=result))
            else:
                await self._session.send(EditTask(task_id=current.id, task=result))

        editor = TaskEditorScreen(
            None if current is None else current.task,
            self._settings or Settings(),
            self._model_choices,
        )
        self.push_screen(editor, saved)

    async def action_run_tasks(self) -> None:
        """Ask the backend to run the task list, unless something is already running."""
        if not self._busy["work"]:
            await self._session.send(RunTasks())

    def _select(self, window_id: str) -> None:
        self.query_one(f"#{self._selected}").remove_class("selected")
        self.query_one(f"#{window_id}").add_class("selected")
        self._selected = window_id

    @on(Tabs.TabActivated, "#conversation-tabs")
    def _show_tab(self, event: Tabs.TabActivated) -> None:
        tabs = self._tabs
        agent = tabs.agent_of(event.tab.id or "")
        tabs.mark_read(agent)
        self._transcript.show_only(agent, event.tab.label_text)

    @on(AgentsPane.Cancel)
    async def _cancel_agent(self, message: AgentsPane.Cancel) -> None:
        await self._session.send(CancelAgent(agent_id=message.agent_id))

    def action_toggle_planner(self) -> None:
        """Show the planner tab, or go back to the tab shown before it."""
        self._tabs.toggle_planner()

    @on(TaskList.Accept)
    async def _accept_task(self, message: TaskList.Accept) -> None:
        await self._session.send(AcceptTask(task_id=message.task_id))

    @on(TaskList.Remove)
    async def _remove_task(self, message: TaskList.Remove) -> None:
        await self._session.send(RemoveTask(task_id=message.task_id))

    async def action_super_review(self) -> None:
        # Reviews have their own lane, so this works while tasks run.
        await self._session.send(RequestSuperReview())

    def action_configure_checks(self) -> None:
        """Ask first, so a stray ctrl+e doesn't queue a configurator."""

        async def answered(confirmed: bool | None) -> None:
            # The backend queues it first and runs it, or says one is already on its way.
            if confirmed:
                await self._session.send(ConfigureChecks())

        self.push_screen(ConfirmScreen(CONFIGURE_TITLE, CONFIGURE_MESSAGE, CONFIGURE_YES), answered)

    async def action_cancel_turn(self) -> None:
        # The planner has its own lane, stopped with ctrl+x; ctrl+s stops the task run.
        await self._session.send(CancelTurn(lane="work"))

    async def action_stop_planner(self) -> None:
        await self._session.send(CancelTurn(lane="planner"))

    def action_pick_model(self) -> None:
        if self._settings is not None:
            models = (None, *self._model_choices)
            candidates = [self._settings.model_copy(update={"model": m}) for m in models]
            self._open_picker(self._model_picker, candidates)

    def action_pick_tool_access(self) -> None:
        if self._settings is not None:
            candidates = [
                self._settings.model_copy(update={"tool_access": a}) for a in TOOL_ACCESS_OPTIONS
            ]
            self._open_picker(self._tool_picker, candidates)

    def _open_picker(self, picker: PickerScreen, candidates: list[Settings]) -> None:
        """Show ``picker``; choosing option ``i`` requests ``candidates[i]``.

        While a picker is open, its modal screen blocks the app's key bindings, so
        a second picker can't be pushed on top.
        """
        picker.current = candidates.index(self._settings) if self._settings in candidates else None

        async def chosen(index: int | None) -> None:
            if index is not None:
                await self._request_settings(candidates[index])

        self.push_screen(picker, chosen)

    async def _request_settings(self, settings: Settings) -> None:
        # The display updates when the backend confirms with SettingsChanged.
        await self._session.send(UpdateSettings(settings=settings))

    @work(exclusive=True, group="events")
    async def _consume_events(self) -> None:
        async for event in self._session.events():
            self._render_event(event)

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
            ):
                self._render_conversation(event)
            case (
                TaskStarted()
                | VerificationStarted()
                | VerificationSkipped()
                | VerificationFinished()
                | TaskMerged()
            ):
                self._render_progress(event)
            case (
                AgentStarted()
                | TaskGaveUp()
                | AgentExited()
                | AgentRequest()
                | AgentReply()
                | ProgressNoted()
                | Notice()
                | StartupChecked()
            ):
                self._append(*activity_entry(event), getattr(event, "agent_id", None))
            case _:
                assert_never(event)

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

    def _render_conversation(
        self,
        event: TurnStarted
        | TurnFinished
        | ErrorOccurred
        | AssistantMessage
        | ToolCallStarted
        | ToolCallFinished,
    ) -> None:
        match event:
            case TurnStarted():
                pass  # The prompt follows BusyChanged, which also covers task runs.
            case TurnFinished():
                self._turn_finished(event)
            case ErrorOccurred(message=message):
                self._append(*agent_labelled((message, "error"), event.agent_id), event.agent_id)
            case AssistantMessage() | ToolCallStarted() | ToolCallFinished():
                self._append(*output_entry(event), event.agent_id)
            case _:
                assert_never(event)

    def _render_progress(
        self,
        event: TaskStarted
        | VerificationStarted
        | VerificationSkipped
        | VerificationFinished
        | TaskMerged,
    ) -> None:
        """Note a task run's progress in the transcript: under its role's tab, and All."""
        if isinstance(event, TaskStarted):
            entry = self._task_header(event.task_id)
        elif isinstance(event, TaskMerged):
            entry = note_entry(event)
        else:
            entry = self._verification_entry(event)
        self._note_task(event.task_id, entry)

    def _verification_entry(
        self, event: VerificationStarted | VerificationSkipped | VerificationFinished
    ) -> tuple[str, EntryKind]:
        match event:
            case VerificationStarted(step=step):
                return verification_started_entry(self._step(step))
            case VerificationSkipped(reason=reason):
                return verification_skipped_entry(reason)
            case VerificationFinished(step=step, passed=passed):
                return verification_finished_entry(self._step(step), passed)
            case _:
                assert_never(event)

    def _follow_agents(self, event: Event) -> None:
        self._agents.track(event)
        self._tabs.track(event)
        self._withdraw_proposal(event)

    def _show_tasks(self, tasks: tuple[TaskState, ...]) -> None:
        task_list = self._task_list
        task_list.show(tasks)
        numbers = dict(zip((t.id for t in task_list.tasks), task_list.numbers, strict=True))
        self._agents.number_tasks(numbers)

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
            self._show_proposal(event)
        else:
            self._usage.show(event)

    def _show_proposal(self, event: ChecksProposed) -> None:
        """Ask the user about ``event``'s proposal; their decision goes to the backend."""
        screen = self._proposal = ProposalScreen(event)

        async def decided(decision: DecideProposal | None) -> None:
            self._proposal = None
            if decision is not None:
                self._append(*decision_entry(event.agent_id, decision), event.agent_id)
                await self._session.send(decision)

        self.push_screen(screen, decided)

    def _withdraw_proposal(self, event: Event) -> None:
        """Close the proposal shown, if ``event`` is its agent exiting."""
        screen = self._proposal
        if isinstance(event, AgentExited) and screen and screen.proposal.agent_id == event.agent_id:
            screen.dismiss(None)

    def _show_settings(self, settings: Settings, choices: tuple[str, ...]) -> None:
        if choices != self._model_choices:
            self._model_picker.set_labels(["default", *choices])
        self._settings, self._model_choices = settings, choices
        self._prompt.show_settings(settings)

    def _set_busy(self, lane: BusyLane, busy: bool) -> None:
        self._busy[lane] = busy
        if lane == "planner":
            # Take prompts only while the planner is free, and mark its tab while it isn't.
            self._prompt.disabled = busy
            self._tabs.set_planner_busy(busy)

    def _turn_finished(self, event: TurnFinished) -> None:
        if event.usage is not None:
            self._task_list.count(event.usage)
        entry = turn_end_entry(event.reason)
        if entry is not None:
            self._append(*agent_labelled(entry, event.agent_id), event.agent_id)

    def _task_header(self, task_id: str) -> tuple[str, EntryKind]:
        """Task ``task_id``'s header, by the number the list shows for it."""
        task_list = self._task_list
        state = task_list.find(task_id)
        number = task_id if state is None else task_list.numbers[task_list.position(task_id)]
        return task_started_entry(number, state)

    def _note_task(self, task_id: str, entry: tuple[str, EntryKind]) -> None:
        """Add ``entry``, a note on task ``task_id``'s run, under the task's role's tab as
        well as All, marking that tab unread when it isn't being shown. A task that isn't
        on the list (removed meanwhile, or the startup check's) is noted under All only."""
        key = task_tab_key(self._task_list.find(task_id))
        self._tabs.mark_unread(key)
        self._transcript.add_entry(*entry, key)

    def _step(self, step: int) -> StepState:
        return self._verification.steps[step]

    def _append(self, text: str, kind: EntryKind, agent: str | None = None) -> None:
        # Entries are kept by tab: a pinned role's agents' under its tab.
        key = self._tabs.key_of(agent)
        self._transcript.add_entry(text, kind, key)
