"""The task list window and the modal that edits one task."""

from collections.abc import Callable, Sequence
from typing import ClassVar, Literal

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static

from orbit.protocol import Settings, TaskSpec, TurnEndReason
from orbit.tui.widgets import (
    TOOL_ACCESS_DESCRIPTIONS,
    TOOL_ACCESS_OPTIONS,
    PickerScreen,
    PromptInput,
)

TaskStatus = Literal["pending", "running"] | TurnEndReason
STATUS_ICONS: dict[TaskStatus, str] = {
    "pending": "○",
    "running": "▶",
    "completed": "✓",
    "cancelled": "■",
    "max_steps": "✗",
    "error": "✗",
}
NEW_TASK_LABEL = "+ new task"


class TaskList(OptionList):
    """Queued tasks plus a trailing "new task" row.

    Like the prompt, it only takes focus once activated (``enter_list_mode``);
    then j/k move, enter picks a row and escape returns to window navigation.
    """

    DEFAULT_CSS = """
    TaskList {
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("escape", "normal_mode", show=False),
    ]

    def __init__(self, *, id: str) -> None:
        super().__init__(id=id)
        self.can_focus = False
        self.tasks: list[TaskSpec] = []
        self.statuses: list[TaskStatus] = []

    def on_mount(self) -> None:
        self.border_title = "Task list"
        self._show()

    def enter_list_mode(self) -> None:
        self.can_focus = True
        self.focus()
        if self.highlighted is None:
            self.highlighted = 0

    def action_normal_mode(self) -> None:
        self.screen.set_focus(None)

    def on_blur(self) -> None:
        self.can_focus = False

    def on_click(self, event: events.Click) -> None:
        # Clicks don't pick rows unless the list is active.
        if not self.has_focus:
            event.prevent_default()

    def set_task(self, index: int, task: TaskSpec) -> None:
        """Replace task ``index``, or add one when ``index`` is the new-task row."""
        if index == len(self.tasks):
            self.tasks.append(task)
            self.statuses.append("pending")
            self._show(highlight=index + 1)
        else:
            self.tasks[index] = task
            self.statuses[index] = "pending"
            self._show()

    def set_status(self, index: int, status: TaskStatus) -> None:
        self.statuses[index] = status
        self._show()

    def _show(self, highlight: int | None = None) -> None:
        if highlight is None:
            highlight = self.highlighted
        self.clear_options()
        self.add_options(
            [task_label(t, s) for t, s in zip(self.tasks, self.statuses, strict=True)]
            + [Text(NEW_TASK_LABEL, style="dim italic")]
        )
        self.highlighted = highlight


def task_label(task: TaskSpec, status: TaskStatus) -> Text:
    model = task.settings.model or "default"
    label = Text(f"{STATUS_ICONS[status]} {task.prompt}\n")
    label.append(f"  {model} · {task.settings.tool_access}", style="dim")
    return label


class FieldBox(Static):
    """A labelled, selectable value in the task editor."""

    DEFAULT_CSS = """
    FieldBox {
        height: 3;
        border: round $secondary;
        padding: 0 1;
    }
    """

    def __init__(self, title: str, value: str, *, id: str) -> None:
        super().__init__(value, id=id)
        self.border_title = title


FIELDS = ("task-prompt", "task-model", "task-tools")


class TaskEditorScreen(ModalScreen[TaskSpec | None]):
    """Edit one task's prompt, model and tool access. Dismisses with the task, or None.

    Works like the main screen: j/k select a field, i types in the prompt (escape
    or enter stops), enter on model or tools opens a picker, ctrl+s saves and
    escape discards.
    """

    AUTO_FOCUS = None
    DEFAULT_CSS = """
    TaskEditorScreen { align: center middle; }
    TaskEditorScreen > Vertical {
        width: 80;
        max-width: 90%;
        height: auto;
        border: round $accent;
        padding: 0 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "move(1)", show=False),
        Binding("k", "move(-1)", show=False),
        Binding("i", "insert_mode", show=False),
        Binding("enter", "choose", show=False),
        Binding("ctrl+s", "save", "Save"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, task: TaskSpec | None, settings: Settings, models: Sequence[str]) -> None:
        super().__init__()
        self.title_text = "New task" if task is None else "Edit task"
        self._prompt = "" if task is None else task.prompt
        self._settings = settings if task is None else task.settings
        self._models = list(models)
        self._field = 0

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = self.title_text
            box.border_subtitle = "j/k move · i type · enter choose · ctrl+s save · esc cancel"
            yield PromptInput(value=self._prompt, placeholder="Press i to type", id="task-prompt")
            yield FieldBox("Model", self._settings.model or "default", id="task-model")
            yield FieldBox("Tools", self._settings.tool_access, id="task-tools")

    def on_mount(self) -> None:
        self.query_one(f"#{FIELDS[0]}").add_class("selected")

    def action_move(self, step: int) -> None:
        self._select(min(max(self._field + step, 0), len(FIELDS) - 1))

    def action_insert_mode(self) -> None:
        self._select(0)
        self.query_one(PromptInput).enter_insert_mode()

    def action_choose(self) -> None:
        if FIELDS[self._field] == "task-model":
            models = [None, *self._models]
            picker = PickerScreen("Model", ["default", *self._models])
            current = self._settings.model
            index = models.index(current) if current in models else None
            self._pick(picker, index, lambda i: self._update(model=models[i]))
        elif FIELDS[self._field] == "task-tools":
            labels = [TOOL_ACCESS_DESCRIPTIONS[a] for a in TOOL_ACCESS_OPTIONS]
            index = TOOL_ACCESS_OPTIONS.index(self._settings.tool_access)
            self._pick(
                PickerScreen("Tool access", labels),
                index,
                lambda i: self._update(tool_access=TOOL_ACCESS_OPTIONS[i]),
            )

    def action_save(self) -> None:
        prompt = self.query_one(PromptInput).value.strip()
        if prompt:
            self.dismiss(TaskSpec(prompt=prompt, settings=self._settings))

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#task-prompt")
    def _stop_typing(self, event: Input.Submitted) -> None:
        event.stop()
        self.query_one(PromptInput).action_normal_mode()

    def _select(self, field: int) -> None:
        self.query_one(f"#{FIELDS[self._field]}").remove_class("selected")
        self.query_one(f"#{FIELDS[field]}").add_class("selected")
        self._field = field

    def _pick(
        self, picker: PickerScreen, current: int | None, apply: Callable[[int], None]
    ) -> None:
        picker.current = current

        def chosen(index: int | None) -> None:
            if index is not None:
                apply(index)

        self.app.push_screen(picker, chosen)

    def _update(self, **changes: object) -> None:
        self._settings = self._settings.model_copy(update=changes)
        self.query_one("#task-model", FieldBox).update(self._settings.model or "default")
        self.query_one("#task-tools", FieldBox).update(self._settings.tool_access)
