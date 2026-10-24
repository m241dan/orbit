"""The modal that edits one task."""

from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from orbit.protocol import Settings, TaskSpec
from orbit.tui.settings_pickers import SettingsPickers
from orbit.tui.view.settings import MODEL_TITLE, TOOL_ACCESS_TITLE
from orbit.tui.view.task_editor import TaskEditor
from orbit.tui.view.tasks import (
    TASK_EDITOR_HINT,
    TASK_PROMPT_TITLE,
    TASK_TITLE_PLACEHOLDER,
    TASK_TITLE_TITLE,
)
from orbit.tui.widgets import PromptBox, PromptInput, mark_selected


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


class TaskEditorScreen(ModalScreen[TaskSpec | None]):
    """Shows the :class:`TaskEditor` of one task's title, prompt, model and tool access.
    Dismisses with the task, or None.

    Works like the main screen: j/k select a field, J/K scroll the prompt box from any field,
    i types in the title or prompt (escape stops; enter stops in the title and adds a line in
    the prompt), enter on model or tools opens its picker, ctrl+s saves and escape discards.
    The pickers are the app's ``pickers``, built once so they open instantly and offering the
    app's current model choices.
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
        pickers: SettingsPickers,
    ) -> None:
        super().__init__()
        self.editor = TaskEditor(self, task, settings, pickers)

    @property
    def title_text(self) -> str:
        """The editor's title, for a new task or one being edited."""
        return self.editor.heading

    def compose(self) -> ComposeResult:
        title, body = self.editor.text
        model, tools = self.editor.settings_shown
        # Fields are reached with j/k, not by tabbing or clicking; the wheel still scrolls.
        with VerticalScroll(can_focus=False) as box:
            box.border_title = self.editor.heading
            box.border_subtitle = TASK_EDITOR_HINT
            yield PromptInput(value=title, placeholder=TASK_TITLE_PLACEHOLDER, id="task-title")
            yield PromptBox(body, id="task-prompt")
            yield FieldBox(MODEL_TITLE, model, id="task-model")
            yield FieldBox(TOOL_ACCESS_TITLE, tools, id="task-tools")

    def on_mount(self) -> None:
        self.query_one(PromptInput).border_title = TASK_TITLE_TITLE
        self.query_one(PromptBox).border_title = TASK_PROMPT_TITLE
        self.query_one(f"#{self.editor.selected}").add_class("selected")

    def action_move(self, step: int) -> None:
        self.editor.move(step)

    def action_scroll_prompt(self, step: int) -> None:
        """Scroll the prompt box ``step`` lines, leaving its cursor and the selection alone."""
        self.query_one(PromptBox).scroll_relative(y=step, animate=False)

    def action_insert_mode(self) -> None:
        self.editor.insert()

    def action_choose(self) -> None:
        self.editor.choose()

    def action_save(self) -> None:
        self.editor.save()

    def action_cancel(self) -> None:
        self.editor.cancel()

    @on(Input.Submitted, "#task-title")
    def _stop_typing(self, event: Input.Submitted) -> None:
        event.stop()
        self.query_one(PromptInput).action_normal_mode()

    @property
    def typed_title(self) -> str:
        return self.query_one(PromptInput).value

    @property
    def typed_body(self) -> str:
        return self.query_one(PromptBox).text

    def select_field(self, previous: str, field: str) -> None:
        mark_selected(self.query_one(f"#{previous}"), False)
        selected = self.query_one(f"#{field}")
        mark_selected(selected, True)
        selected.scroll_visible()

    def type_title(self) -> None:
        self.query_one(PromptInput).enter_insert_mode()

    def type_body(self) -> None:
        self.query_one(PromptBox).enter_insert_mode()

    def show_settings(self, model: str, tools: str) -> None:
        self.query_one("#task-model", FieldBox).update(model)
        self.query_one("#task-tools", FieldBox).update(tools)
