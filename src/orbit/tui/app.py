"""Textual frontend. Talks to the backend only through a protocol Session."""

import json
from typing import ClassVar, assert_never

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Input, OptionList

from orbit.protocol import (
    AddTask,
    AssistantMessage,
    BusyChanged,
    CancelTurn,
    EditTask,
    ErrorOccurred,
    Event,
    RunTasks,
    Session,
    Settings,
    SettingsChanged,
    SkipReason,
    SubmitPrompt,
    TasksChanged,
    TaskSpec,
    TaskStarted,
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
from orbit.tui.tasks import TaskEditorScreen, TaskList
from orbit.tui.widgets import (
    TOOL_ACCESS_DESCRIPTIONS,
    TOOL_ACCESS_OPTIONS,
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
    "verification-steps": {"h": "prompt", "k": "task-list"},
}


SKIP_REASONS: dict[SkipReason, str] = {
    "tools_off": "tools were off",
    "no_changes": "no files changed",
}


def output_entry(event: AssistantMessage | ToolCallStarted | ToolCallFinished) -> tuple[str, str]:
    """A piece of a turn's output as transcript text and the kind of entry to show it as."""
    match event:
        case AssistantMessage(text=text):
            return text, "assistant"
        case ToolCallStarted(name=name, arguments=arguments):
            return f"→ {name} {json.dumps(arguments)}", "tool"
        case ToolCallFinished(output=output, is_error=True):
            return output, "error"
        case ToolCallFinished(output=output):
            return output, "tool"
        case _:
            assert_never(event)


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
        Binding("i", "insert_mode", "Type"),
        Binding("ctrl+r", "run_tasks", "Run tasks"),
        Binding("escape", "cancel_turn", "Cancel turn"),
        Binding("ctrl+o", "pick_model", "Model"),
        Binding("ctrl+l", "pick_tool_access", "Tools"),
    ]

    def __init__(self, session: Session) -> None:
        super().__init__()
        self._session = session
        # Id of the window h/j/k/l last moved to.
        self._selected = "prompt"
        # Last settings reported by the backend; None until the first report.
        self._settings: Settings | None = None
        self._model_choices: tuple[str, ...] = ()
        # Pickers are built once and installed so they open instantly.
        self._model_picker = PickerScreen("Model", ["default"])
        self._tool_picker = PickerScreen(
            "Tool access", [TOOL_ACCESS_DESCRIPTIONS[a] for a in TOOL_ACCESS_OPTIONS]
        )

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical():
                yield Transcript(id="transcript")
                yield PromptInput(placeholder="Press i to type, esc to stop", id="prompt")
            with Vertical(id="side"):
                yield TaskList(id="task-list")
                yield VerificationPanel(id="verification-steps")
        yield Footer()

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
        prompt.disabled = True
        self._append(text, "user")
        await self._session.send(SubmitPrompt(text=text))

    # Window navigation only applies at the top level, when no window is active.

    def action_move(self, direction: str) -> None:
        target = NEIGHBORS[self._selected].get(direction)
        if target is not None and self.focused is None:
            self._select(target)

    def action_activate(self) -> None:
        if self._selected == "task-list":
            self.query_one(TaskList).enter_list_mode()

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
        if not self.query_one(PromptInput).disabled:
            await self._session.send(RunTasks())

    def _select(self, window_id: str) -> None:
        self.query_one(f"#{self._selected}").remove_class("selected")
        self.query_one(f"#{window_id}").add_class("selected")
        self._selected = window_id

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
        match event:
            case SettingsChanged() | BusyChanged() | TasksChanged() | VerificationChanged():
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
            case _:
                assert_never(event)

    def _render_state(
        self, event: SettingsChanged | BusyChanged | TasksChanged | VerificationChanged
    ) -> None:
        """Show a snapshot of backend state."""
        match event:
            case SettingsChanged(settings=settings, model_choices=choices):
                self._show_settings(settings, choices)
            case BusyChanged(busy=busy):
                self.query_one(PromptInput).disabled = busy
            case TasksChanged(tasks=tasks):
                self.query_one(TaskList).show(tasks)
            case VerificationChanged(steps=steps):
                self.query_one(VerificationPanel).show(steps)
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
                self._append(*output_entry(event))
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
                self._append(f"🔍 Verifying: {self._step_name(step)}…", "verify")
            case VerificationSkipped(reason=reason):
                self._append(f"⏩ Verification skipped: {SKIP_REASONS[reason]}", "verify")
            case VerificationFinished(step=step, passed=passed):
                self._verification_finished(step, passed)
            case _:
                assert_never(event)

    def _show_settings(self, settings: Settings, choices: tuple[str, ...]) -> None:
        if choices != self._model_choices:
            self._model_picker.set_labels(["default", *choices])
        self._settings, self._model_choices = settings, choices
        self.query_one(PromptInput).show_settings(settings)

    def _turn_finished(self, reason: TurnEndReason) -> None:
        if reason not in ("completed", "error"):
            self._append(f"[turn ended: {reason}]", "tool")

    def _announce_task(self, task_id: str) -> None:
        task_list = self.query_one(TaskList)
        index = task_list.position(task_id)
        prompt = task_list.tasks[index].task.prompt
        self._append(f"Task {task_list.numbers[index]}: {prompt}", "user")

    def _step_name(self, step: int) -> str:
        return self.query_one(VerificationPanel).steps[step].name

    def _verification_finished(self, step: int, passed: bool) -> None:
        if passed:
            self._append(f"✅ Verification passed: {self._step_name(step)}", "verify")
        else:
            self._append(f"❌ Verification failed: {self._step_name(step)}", "error")

    def _append(self, text: str, kind: str) -> None:
        self.query_one(Transcript).add_entry(text, kind)
