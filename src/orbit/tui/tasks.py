"""The task list window and the modal that edits one task."""

import time
from collections.abc import Callable, Sequence
from typing import ClassVar

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from orbit.protocol import Settings, TaskSpec, TaskState, Usage
from orbit.tui.view import (
    SPINNER_FPS,
    TOOL_ACCESS_DESCRIPTIONS,
    TOOL_ACCESS_OPTIONS,
    join_prompt,
    model_label,
    spinner_frame,
    split_prompt,
    task_label,
    task_numbers,
    usage_label,
)
from orbit.tui.widgets import ListPane, PickerScreen, PromptBox, PromptInput

NEW_TASK_LABEL = "+ new task"


class TaskList(ListPane):
    """The backend's task list plus a trailing "new task" row.

    Like the prompt, it only takes focus once activated (``enter_list_mode``);
    then j/k move, enter picks a row, ``a`` accepts a draft, ``x`` removes a task and
    escape returns to window navigation. Its border shows what the session's turns cost.
    """

    class Accept(Message):
        """The user accepted draft task ``task_id``."""

        def __init__(self, task_id: str) -> None:
            super().__init__()
            self.task_id = task_id

    class Remove(Message):
        """The user asked to remove task ``task_id``."""

        def __init__(self, task_id: str) -> None:
            super().__init__()
            self.task_id = task_id

    DEFAULT_CSS = """
    TaskList {
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("a", "accept", show=False),
        Binding("x", "remove", show=False),
    ]

    def __init__(self, *, id: str) -> None:
        super().__init__(id=id)
        self.tasks: tuple[TaskState, ...] = ()
        self.numbers: list[str] = []
        self.clock: Callable[[], float] = time.monotonic
        self.session_usage = Usage()

    def on_mount(self) -> None:
        self.border_title = "Task list"
        self._show(None)
        self.set_interval(1 / SPINNER_FPS, self.tick)

    def action_accept(self) -> None:
        state = self._highlighted_task()
        if state is not None and state.status == "draft":
            self.post_message(self.Accept(state.id))

    def action_remove(self) -> None:
        state = self._highlighted_task()
        if state is not None:
            self.post_message(self.Remove(state.id))

    def _highlighted_task(self) -> TaskState | None:
        """The task the highlight is on; ``None`` on the new-task row."""
        index = self.highlighted
        return self.tasks[index] if index is not None and index < len(self.tasks) else None

    def on_click(self, event: events.Click) -> None:
        # Clicks don't pick rows unless the list is active.
        if not self.has_focus:
            event.prevent_default()

    def show(self, tasks: Sequence[TaskState]) -> None:
        """Show ``tasks``; the highlight stays on its task, or on the new-task row.

        A task that changed has its row relabelled in place; only tasks coming, going or
        moving rebuild the rows.
        """
        tasks = tuple(tasks)
        if tasks == self.tasks:
            return
        highlight = self._moved_highlight(tasks)
        before = tuple(zip(self.tasks, self.numbers, strict=True))
        self.tasks, self.numbers = tasks, task_numbers(tasks)
        if [state.id for state, _ in before] == [state.id for state in tasks]:
            self._relabel(before)
        else:
            self._show(highlight)

    def _moved_highlight(self, tasks: Sequence[TaskState]) -> int | None:
        """Where the highlight goes once ``tasks`` are shown: with its task, or on the
        new-task row."""
        highlight = self.highlighted
        if highlight is None:
            return None
        if highlight >= len(self.tasks):
            return len(tasks)
        positions = {t.id: i for i, t in enumerate(tasks)}
        return positions.get(self.tasks[highlight].id, highlight)

    def _relabel(self, before: Sequence[tuple[TaskState, str]]) -> None:
        """Relabel the rows whose task or number differs from ``before``, the same tasks in
        the same order."""
        frame = spinner_frame(self.clock())
        rows = zip(self.tasks, self.numbers, strict=True)
        for index, (row, old) in enumerate(zip(rows, before, strict=True)):
            if row != old:
                self.replace_option_prompt_at_index(index, task_label(*row, frame))

    def count(self, usage: Usage) -> None:
        """Add what a turn cost to the session's total."""
        self.session_usage += usage
        self.border_subtitle = f"session: {usage_label(self.session_usage)}"

    def position(self, task_id: str) -> int:
        return next(i for i, t in enumerate(self.tasks) if t.id == task_id)

    def find(self, task_id: str) -> TaskState | None:
        """The task with id ``task_id``, if it's on the list."""
        return next((t for t in self.tasks if t.id == task_id), None)

    def tick(self) -> None:
        """Turn the spinners: redraw just the rows of running tasks."""
        frame = spinner_frame(self.clock())
        for index, (state, number) in enumerate(zip(self.tasks, self.numbers, strict=True)):
            if state.status == "running":
                self.replace_option_prompt_at_index(index, task_label(state, number, frame))

    def _show(self, highlight: int | None) -> None:
        frame = spinner_frame(self.clock())
        self.clear_options()
        self.add_options(
            [task_label(t, n, frame) for t, n in zip(self.tasks, self.numbers, strict=True)]
            + [Text(NEW_TASK_LABEL, style="dim italic")]
        )
        self.highlighted = highlight


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
    ``join_prompt``. Works like the main screen: j/k select a field, i types in the title or
    prompt (escape stops; enter stops in the title and adds a line in the prompt), enter on
    model or tools opens a picker, ctrl+s saves and escape discards.
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
        self._role = TaskSpec.model_fields["role"].default if task is None else task.role
        self._models = list(models)
        self._field = 0

    def compose(self) -> ComposeResult:
        title, body = split_prompt(self._prompt)
        with VerticalScroll() as box:
            box.border_title = self.title_text
            box.border_subtitle = "j/k move · i type · enter choose · ctrl+s save · esc cancel"
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
        models = [None, *self._models]
        picker = PickerScreen("Model", ["default", *self._models])
        current = self._settings.model
        index = models.index(current) if current in models else None
        self._pick(picker, index, lambda i: self._update(model=models[i]))

    def _choose_tools(self) -> None:
        labels = [TOOL_ACCESS_DESCRIPTIONS[a] for a in TOOL_ACCESS_OPTIONS]
        index = TOOL_ACCESS_OPTIONS.index(self._settings.tool_access)
        self._pick(
            PickerScreen("Tool access", labels),
            index,
            lambda i: self._update(tool_access=TOOL_ACCESS_OPTIONS[i]),
        )

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
        self.query_one(f"#{FIELDS[self._field]}").remove_class("selected")
        selected = self.query_one(f"#{FIELDS[field]}")
        selected.add_class("selected")
        selected.scroll_visible()
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
        self.query_one("#task-model", FieldBox).update(model_label(self._settings))
        self.query_one("#task-tools", FieldBox).update(self._settings.tool_access)
