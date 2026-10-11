"""Textual frontend. Talks to the backend only through a protocol Session."""

import json
from typing import ClassVar, assert_never

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Input, OptionList

from orbit.protocol import (
    AssistantMessage,
    CancelTurn,
    ErrorOccurred,
    Event,
    RunTasks,
    Session,
    Settings,
    SettingsChanged,
    SubmitPrompt,
    TaskFinished,
    TaskSpec,
    TaskStarted,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
    UpdateSettings,
)
from orbit.tui.tasks import TaskEditorScreen, TaskList
from orbit.tui.widgets import (
    TOOL_ACCESS_DESCRIPTIONS,
    TOOL_ACCESS_OPTIONS,
    PickerScreen,
    PromptInput,
    SidePanel,
    Transcript,
)

# Window reached from each window by h/j/k/l; missing means an edge.
# Conversation over prompt on the left, task list over verification steps on the right.
NEIGHBORS: dict[str, dict[str, str]] = {
    "transcript": {"j": "prompt", "l": "task-list"},
    "prompt": {"k": "transcript", "l": "verification-steps"},
    "task-list": {"h": "transcript", "j": "verification-steps"},
    "verification-steps": {"h": "prompt", "k": "task-list"},
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
        # Task-list index of each task in the running RunTasks; None when none runs.
        self._batch: list[int] | None = None
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
                yield SidePanel("Verification steps", id="verification-steps")
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
        tasks = self.query_one(TaskList)
        index = event.option_index
        task = tasks.tasks[index] if index < len(tasks.tasks) else None

        def saved(result: TaskSpec | None) -> None:
            if result is not None:
                tasks.set_task(index, result)

        editor = TaskEditorScreen(task, self._settings or Settings(), self._model_choices)
        self.push_screen(editor, saved)

    async def action_run_tasks(self) -> None:
        """Run every task that hasn't completed, unless something is already running."""
        tasks = self.query_one(TaskList)
        prompt = self.query_one(PromptInput)
        batch = [i for i, s in enumerate(tasks.statuses) if s != "completed"]
        if not batch or prompt.disabled:
            return
        self._batch = batch
        prompt.disabled = True
        await self._session.send(RunTasks(tasks=tuple(tasks.tasks[i] for i in batch)))

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
            case SettingsChanged(settings=settings, model_choices=choices):
                if choices != self._model_choices:
                    self._model_picker.set_labels(["default", *choices])
                self._settings, self._model_choices = settings, choices
                self.query_one(PromptInput).show_settings(settings)
            case TurnStarted():
                self.query_one(PromptInput).disabled = True
            case AssistantMessage(text=text):
                self._append(text, "assistant")
            case ToolCallStarted(name=name, arguments=arguments):
                self._append(f"→ {name} {json.dumps(arguments)}", "tool")
            case ToolCallFinished(output=output, is_error=is_error):
                self._append(output, "error" if is_error else "tool")
            case TurnFinished(reason=reason):
                if reason not in ("completed", "error"):
                    self._append(f"[turn ended: {reason}]", "tool")
                if self._batch is None:
                    self.query_one(PromptInput).disabled = False
            case TaskStarted(index=index):
                assert self._batch is not None
                tasks = self.query_one(TaskList)
                position = self._batch[index]
                tasks.set_status(position, "running")
                self._append(f"Task {position + 1}: {tasks.tasks[position].prompt}", "user")
            case TaskFinished(index=index, reason=reason):
                assert self._batch is not None
                self.query_one(TaskList).set_status(self._batch[index], reason)
                if reason == "cancelled" or index == len(self._batch) - 1:
                    self._batch = None
                    self.query_one(PromptInput).disabled = False
            case ErrorOccurred(message=message):
                self._append(message, "error")
            case _:
                assert_never(event)

    def _append(self, text: str, kind: str) -> None:
        self.query_one(Transcript).add_entry(text, kind)
