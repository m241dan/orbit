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
    CancelAgent,
    CancelTurn,
    ChecksProposed,
    ConfigureChecks,
    DecideProposal,
    EditTask,
    ErrorOccurred,
    Event,
    Notice,
    PlanningChanged,
    ProgressNoted,
    RemoveTask,
    RequestSuperReview,
    ReviewStatus,
    RunTasks,
    Session,
    SetPlanning,
    Settings,
    SettingsChanged,
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
    TurnEndReason,
    TurnFinished,
    TurnStarted,
    UpdateSettings,
    VerificationChanged,
    VerificationFinished,
    VerificationSkipped,
    VerificationStarted,
)
from orbit.tui.agents import AgentsPane
from orbit.tui.proposals import ProposalScreen
from orbit.tui.tasks import TaskEditorScreen, TaskList
from orbit.tui.view import (
    TOOL_ACCESS_DESCRIPTIONS,
    TOOL_ACCESS_OPTIONS,
    EntryKind,
    activity_entry,
    decision_entry,
    output_entry,
    task_started_entry,
    turn_end_entry,
    verification_finished_entry,
    verification_skipped_entry,
    verification_started_entry,
)
from orbit.tui.widgets import (
    ConversationTabs,
    PickerScreen,
    PromptInput,
    Transcript,
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
    PromptInput:focus, TaskList:focus { border: heavy $accent; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        # Priority, so it beats the prompt's own ctrl+c (copy).
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
        Binding("h", "move('h')", show=False),
        Binding("j", "move('j')", show=False),
        Binding("k", "move('k')", show=False),
        Binding("l", "move('l')", show=False),
        Binding("enter", "activate", show=False),
        # Priority, to beat the screen's focus_next; check_action lets it through
        # to that everywhere but normal mode.
        Binding("tab", "cycle_tab(1)", "Next agent", key_display="Tab", priority=True),
        Binding("shift+tab", "cycle_tab(-1)", show=False, priority=True),
        Binding("i", "insert_mode", "Type"),
        Binding("ctrl+r", "run_tasks", "Run tasks"),
        Binding("ctrl+g", "super_review", "Super review"),
        Binding("ctrl+e", "configure_checks", "Set up checks"),
        Binding("ctrl+t", "toggle_planning", "Plan"),
        Binding("escape", "cancel_turn", "Cancel turn"),
        Binding("ctrl+o", "pick_model", "Model"),
        Binding("ctrl+l", "pick_tool_access", "Tools"),
    ]

    def __init__(self, session: Session) -> None:
        super().__init__()
        self._session = session
        # Id of the window h/j/k/l last moved to.
        self._selected = "prompt"
        # Whether a turn or task run is in progress, as last reported (or just requested).
        self._busy = False
        self._planning = False
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
                yield PromptInput(placeholder="Press i to type, esc to stop", id="prompt")
            with Vertical(id="side"):
                yield TaskList(id="task-list")
                yield VerificationPanel(id="verification-steps")
                yield AgentsPane(id="agents")
        yield Footer()

    async def on_unmount(self) -> None:
        await self._session.close()

    def on_mount(self) -> None:
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
        prompt = self.query_one(PromptInput)
        prompt.action_normal_mode()
        # Busy from now, not from TurnStarted, so ctrl+r can't race the turn.
        self._set_busy(True)
        self._append(text, "user")
        await self._session.send(SubmitPrompt(text=text))

    # Window navigation only applies at the top level, when no window is active.

    def action_move(self, direction: str) -> None:
        target = NEIGHBORS[self._selected].get(direction)
        if target is not None and self.focused is None:
            self._select(target)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "cycle_tab":
            return self._normal_mode()
        return True

    def _normal_mode(self) -> bool:
        """Nothing focused on the main screen: no typing, list mode or modal screen."""
        return len(self.screen_stack) == 1 and self.focused is None

    def action_cycle_tab(self, step: int) -> None:
        tabs = self.query_one(ConversationTabs)
        if step > 0:
            tabs.action_next_tab()
        else:
            tabs.action_previous_tab()

    def action_activate(self) -> None:
        if self._selected == "task-list":
            self.query_one(TaskList).enter_list_mode()
        elif self._selected == "agents":
            self.query_one(AgentsPane).enter_list_mode()

    def action_insert_mode(self) -> None:
        """Shortcut from any window: select the prompt and start typing."""
        if self.focused is None:
            self._select("prompt")
            self.query_one(PromptInput).enter_insert_mode()

    @on(OptionList.OptionSelected, "#task-list")
    def _edit_task(self, event: OptionList.OptionSelected) -> None:
        tasks = self.query_one(TaskList).tasks
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
        if not self._busy:
            await self._session.send(RunTasks())

    def _select(self, window_id: str) -> None:
        self.query_one(f"#{self._selected}").remove_class("selected")
        self.query_one(f"#{window_id}").add_class("selected")
        self._selected = window_id

    @on(Tabs.TabActivated, "#conversation-tabs")
    def _show_tab(self, event: Tabs.TabActivated) -> None:
        tabs = self.query_one(ConversationTabs)
        agent = tabs.agent_of(event.tab.id or "")
        tabs.mark_read(agent)
        self.query_one(Transcript).show_only(agent, event.tab.label_text)

    @on(AgentsPane.Cancel)
    async def _cancel_agent(self, message: AgentsPane.Cancel) -> None:
        await self._session.send(CancelAgent(agent_id=message.agent_id))

    async def action_toggle_planning(self) -> None:
        await self._session.send(SetPlanning(on=not self._planning))

    @on(TaskList.Accept)
    async def _accept_task(self, message: TaskList.Accept) -> None:
        await self._session.send(AcceptTask(task_id=message.task_id))

    @on(TaskList.Remove)
    async def _remove_task(self, message: TaskList.Remove) -> None:
        await self._session.send(RemoveTask(task_id=message.task_id))

    async def action_super_review(self) -> None:
        # Reviews have their own lane, so this works while tasks run.
        await self._session.send(RequestSuperReview())

    async def action_configure_checks(self) -> None:
        # The backend queues it first and runs it, or says one is already on its way.
        await self._session.send(ConfigureChecks())

    async def action_cancel_turn(self) -> None:
        await self._session.send(CancelTurn())

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
                | PlanningChanged()
                | ReviewStatus()
                | ChecksProposed()
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
            ):
                self._render_progress(event)
            case (
                AgentStarted()
                | TaskGaveUp()
                | AgentExited()
                | AgentRequest()
                | AgentReply()
                | ProgressNoted()
                | TaskMerged()
                | Notice()
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
        | PlanningChanged
        | ReviewStatus
        | ChecksProposed,
    ) -> None:
        """Show a snapshot of backend state, or a decision it waits for."""
        match event:
            case SettingsChanged() | PlanningChanged():
                self._show_session(event)
            case BusyChanged(busy=busy):
                self._set_busy(busy)
            case TasksChanged(tasks=tasks):
                self._show_tasks(tasks)
            case VerificationChanged() | ReviewStatus() | ChecksProposed():
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
            case TurnFinished(reason=reason):
                self._turn_finished(reason)
            case ErrorOccurred(message=message):
                self._append(message, "error")
            case AssistantMessage() | ToolCallStarted() | ToolCallFinished():
                self._append(*output_entry(event), event.agent_id)
            case _:
                assert_never(event)

    def _render_progress(
        self,
        event: TaskStarted | VerificationStarted | VerificationSkipped | VerificationFinished,
    ) -> None:
        """Note a task run's progress in the transcript."""
        match event:
            case TaskStarted(task_id=task_id):
                self._announce_task(task_id)
            case VerificationStarted(step=step):
                self._append(*verification_started_entry(self._step(step)))
            case VerificationSkipped(reason=reason):
                self._append(*verification_skipped_entry(reason))
            case VerificationFinished(step=step, passed=passed):
                self._append(*verification_finished_entry(self._step(step), passed))
            case _:
                assert_never(event)

    def _follow_agents(self, event: Event) -> None:
        self.query_one(AgentsPane).track(event)
        self.query_one(ConversationTabs).track(event)
        self._withdraw_proposal(event)

    def _show_tasks(self, tasks: tuple[TaskState, ...]) -> None:
        task_list = self.query_one(TaskList)
        task_list.show(tasks)
        numbers = dict(zip((t.id for t in task_list.tasks), task_list.numbers, strict=True))
        self.query_one(AgentsPane).number_tasks(numbers)

    def _show_progress(self, event: VerificationChanged | ReviewStatus | ChecksProposed) -> None:
        """Show the verification steps' progress, whether review is paused, or proposed steps."""
        if isinstance(event, VerificationChanged):
            self.query_one(VerificationPanel).show(event.steps)
        elif isinstance(event, ReviewStatus):
            self.query_one(AgentsPane).show_review(event)
        else:
            self._show_proposal(event)

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

    def _show_session(self, event: SettingsChanged | PlanningChanged) -> None:
        """Show the session's settings, or whether prompts go to the planner."""
        if isinstance(event, SettingsChanged):
            self._show_settings(event.settings, event.model_choices)
        else:
            self._planning = event.on
            self.query_one(PromptInput).show_planning(event.on)

    def _show_settings(self, settings: Settings, choices: tuple[str, ...]) -> None:
        if choices != self._model_choices:
            self._model_picker.set_labels(["default", *choices])
        self._settings, self._model_choices = settings, choices
        self.query_one(PromptInput).show_settings(settings)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.query_one(PromptInput).disabled = busy

    def _turn_finished(self, reason: TurnEndReason) -> None:
        entry = turn_end_entry(reason)
        if entry is not None:
            self._append(*entry)

    def _announce_task(self, task_id: str) -> None:
        task_list = self.query_one(TaskList)
        index = task_list.position(task_id)
        self._append(*task_started_entry(task_list.numbers[index], task_list.tasks[index]))

    def _step(self, step: int) -> StepState:
        return self.query_one(VerificationPanel).steps[step]

    def _append(self, text: str, kind: EntryKind, agent: str | None = None) -> None:
        self.query_one(Transcript).add_entry(text, kind, agent)
