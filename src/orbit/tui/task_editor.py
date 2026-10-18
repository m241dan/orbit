"""The modal that edits one task."""

from collections.abc import Callable, Sequence
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from orbit.protocol import Settings, TaskSpec
from orbit.tui.view import (
    TASK_EDITOR_HINT,
    join_prompt,
    model_choices,
    model_label,
    split_prompt,
    task_editor_title,
    tool_access_choices,
)
from orbit.tui.widgets import PickerScreen, PromptBox, PromptInput, mark_selected


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


FIELDS = ("task-title", "task-prompt", "task-model", "task-tools")
TEXT_FIELDS = FIELDS[:2]


class TaskEditorScreen(ModalScreen[TaskSpec | None]):
    """Edit one task's title, prompt, model and tool access. Dismisses with the task, or None.

    The title is the prompt's first line and the prompt box its body, joined on save by
    ``join_prompt``. Works like the main screen: j/k select a field, J/K scroll the prompt box
    from any field, i types in the title or prompt (escape stops; enter stops in the title and
    adds a line in the prompt), enter on model or tools opens its picker, ctrl+s saves and
    escape discards. The pickers are the app's model and tool access ``pickers``, built once
    so they open instantly; ``models`` returns the app's current model choices, which the
    model picker's labels follow.
    """

    AUTO_FOCUS = None
    DEFAULT_CSS = """
    TaskEditorScreen { align: center middle; }
    TaskEditorScreen > VerticalScroll {
        width: 80;
        max-width: 90%;
        height: auto;
        max-height: 90%;
        border: round $accent;
        padding: 0 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "move(1)", show=False),
        Binding("k", "move(-1)", show=False),
        Binding("J", "scroll_prompt(1)", show=False),
        Binding("K", "scroll_prompt(-1)", show=False),
        Binding("i", "insert_mode", show=False),
        Binding("enter", "choose", show=False),
        Binding("ctrl+s", "save", "Save"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        task: TaskSpec | None,
        settings: Settings,
        models: Callable[[], Sequence[str]],
        pickers: tuple[PickerScreen, PickerScreen],
    ) -> None:
        super().__init__()
        self.title_text = task_editor_title(new=task is None)
        self._prompt = "" if task is None else task.prompt
        self._settings = settings if task is None else task.settings
        self._role = TaskSpec.model_fields["role"].default if task is None else task.role
        self._models = models
        self._model_picker, self._tool_picker = pickers
        self._field = 0

    def compose(self) -> ComposeResult:
        title, body = split_prompt(self._prompt)
        # Fields are reached with j/k, not by tabbing or clicking; the wheel still scrolls.
        with VerticalScroll(can_focus=False) as box:
            box.border_title = self.title_text
            box.border_subtitle = TASK_EDITOR_HINT
            yield PromptInput(value=title, placeholder="Press i to type", id="task-title")
            yield PromptBox(body, id="task-prompt")
            yield FieldBox("Model", model_label(self._settings), id="task-model")
            yield FieldBox("Tools", self._settings.tool_access, id="task-tools")

    def on_mount(self) -> None:
        self.query_one(PromptInput).border_title = "Title"
        self.query_one(PromptBox).border_title = "Prompt"
        self.query_one(f"#{FIELDS[0]}").add_class("selected")

    def action_move(self, step: int) -> None:
        self._select(min(max(self._field + step, 0), len(FIELDS) - 1))

    def action_scroll_prompt(self, step: int) -> None:
        """Scroll the prompt box ``step`` lines, leaving its cursor and the selection alone."""
        self.query_one(PromptBox).scroll_relative(y=step, animate=False)

    def action_insert_mode(self) -> None:
        """Type in the selected text field, or in the title when another is selected."""
        if FIELDS[self._field] not in TEXT_FIELDS:
            self._select(0)
        if FIELDS[self._field] == "task-title":
            self.query_one(PromptInput).enter_insert_mode()
        else:
            self.query_one(PromptBox).enter_insert_mode()

    def action_choose(self) -> None:
        if FIELDS[self._field] == "task-model":
            self._choose_model()
        elif FIELDS[self._field] == "task-tools":
            self._choose_tools()

    def _choose_model(self) -> None:
        options, current = model_choices(self._models(), self._settings)
        self._model_picker.choose(options, current, self._update)

    def _choose_tools(self) -> None:
        options, current = tool_access_choices(self._settings)
        self._tool_picker.choose(options, current, self._update)

    def action_save(self) -> None:
        """Save the task, if it has a title; its body may be empty."""
        title = self.query_one(PromptInput).value
        if title.strip():
            prompt = join_prompt(title, self.query_one(PromptBox).text)
            self.dismiss(TaskSpec(prompt=prompt, settings=self._settings, role=self._role))

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#task-title")
    def _stop_typing(self, event: Input.Submitted) -> None:
        event.stop()
        self.query_one(PromptInput).action_normal_mode()

    def _select(self, field: int) -> None:
        """Select field ``field``, scrolling it into view when the editor doesn't all fit."""
        mark_selected(self.query_one(f"#{FIELDS[self._field]}"), False)
        selected = self.query_one(f"#{FIELDS[field]}")
        mark_selected(selected, True)
        selected.scroll_visible()
        self._field = field

    def _update(self, settings: Settings) -> None:
        self._settings = settings
        self.query_one("#task-model", FieldBox).update(model_label(self._settings))
        self.query_one("#task-tools", FieldBox).update(self._settings.tool_access)
