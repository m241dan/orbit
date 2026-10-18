"""The task list window, the modal that edits one task and the one that says why a task
ended."""

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
from textual.widgets.option_list import Option

from orbit.protocol import Settings, TaskSpec, TaskState, Usage
from orbit.tui.view import (
    NEW_TASK_LABEL,
    REASON_HINT,
    TASK_EDITOR_HINT,
    TOOL_ACCESS_OPTIONS,
    active_task,
    copied_note,
    follow_top,
    join_prompt,
    model_label,
    model_options,
    reason_text,
    reason_title,
    session_usage_subtitle,
    spinner_frame,
    split_prompt,
    task_editor_title,
    task_label,
    task_list_title,
    task_numbers,
)
from orbit.tui.widgets import (
    ListPane,
    PickerScreen,
    PromptBox,
    PromptInput,
    Ticker,
    mark_selected,
)


class TaskList(ListPane):
    """The backend's task list plus a trailing "new task" row.

    Like the prompt, it only takes focus once activated (``enter_list_mode``);
    then j/k move, enter picks a row, ``a`` accepts a draft, ``x`` removes a task, ``f`` shows
    why a task ended and escape returns to window navigation. Its border shows what the
    session's turns cost. While following, it scrolls to keep the active task
    (``active_task``) in view; activating it or scrolling it with the mouse wheel holds it
    where it is, until ``let_go``.
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

    class ShowReason(Message):
        """The user asked why ``state``, task ``number`` on the list, ended."""

        def __init__(self, state: TaskState, number: str) -> None:
            super().__init__()
            self.state = state
            self.number = number

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
        Binding("f", "show_reason", show=False),
    ]

    def __init__(self, *, id: str) -> None:
        super().__init__(id=id)
        self.tasks: tuple[TaskState, ...] = ()
        self.numbers: list[str] = []
        self.numbers_by_id: dict[str, str] = {}
        self.clock: Callable[[], float] = time.monotonic
        self.session_usage = Usage()
        self._ticker = Ticker(self, self.tick)
        self.following = True

    def on_mount(self) -> None:
        self.border_title = task_list_title(held=False)
        self._redraw()

    def on_resize(self) -> None:
        # Reasons are cut to the list's width, so a new width relabels their rows. OptionList
        # re-measures its rows in its own resize handler, which runs after this one.
        self._redraw()
        self.call_after_refresh(self._follow)

    def enter_list_mode(self) -> None:
        """Activate and hold the list, with the highlight on the active task if there is one."""
        self._hold()
        super().enter_list_mode()
        index = active_task(self.tasks)
        if index is not None and self.can_focus:
            self.highlighted = index

    def on_mouse_scroll_down(self) -> None:
        self._hold()

    def on_mouse_scroll_up(self) -> None:
        self._hold()

    def _hold(self) -> None:
        """Stop following, so the list stays where the user put it."""
        self.following = False
        self.border_title = task_list_title(held=True)

    def let_go(self) -> None:
        """Follow the active task again, scrolling to it at once; nothing if not held."""
        if self.following:
            return
        self.following = True
        self.border_title = task_list_title(held=False)
        self._follow()

    def action_accept(self) -> None:
        state = self._highlighted_task()
        if state is not None and state.status == "draft":
            self.post_message(self.Accept(state.id))

    def action_remove(self) -> None:
        state = self._highlighted_task()
        if state is not None:
            self.post_message(self.Remove(state.id))

    def action_show_reason(self) -> None:
        state = self._highlighted_task()
        if state is not None and state.reason:
            self.post_message(self.ShowReason(state, self.numbers_by_id[state.id]))

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
        self.tasks, self.numbers = tasks, task_numbers(tasks)
        self.numbers_by_id = dict(zip((t.id for t in tasks), self.numbers, strict=True))
        self._redraw()
        self._ticker.keep(any(t.status == "running" for t in tasks))
        self._follow()

    def _follow(self) -> None:
        """Scroll just enough to bring the active task into view, if following; the
        highlight stays where it is."""
        index = active_task(self.tasks)
        if not self.following or index is None:
            return
        rows = self.row_lines()
        if index >= len(rows):  # Not measured yet, before the list has a size.
            return
        top = follow_top(rows, index, round(self.scroll_y), self.scrollable_content_region.height)
        if top is not None:
            self.scroll_to(y=top, animate=False, immediate=True)

    @property
    def ticking(self) -> bool:
        """Whether the spinners of running tasks are turning."""
        return self._ticker.ticking

    def count(self, usage: Usage) -> None:
        """Add what a turn cost to the session's total."""
        self.session_usage += usage
        self.border_subtitle = session_usage_subtitle(self.session_usage)

    def reset_usage(self, usage: Usage) -> None:
        """Start the session's total afresh at ``usage``; a session that has spent nothing
        shows no total."""
        self.session_usage = usage
        self.border_subtitle = session_usage_subtitle(usage)

    def find(self, task_id: str) -> TaskState | None:
        """The task with id ``task_id``, if it's on the list."""
        return next((t for t in self.tasks if t.id == task_id), None)

    def tick(self) -> None:
        """Turn the spinners: redraw just the rows of running tasks."""
        self._redraw()

    def _redraw(self) -> None:
        """Show the tasks, relabelling in place the rows whose label changed."""
        frame, width = spinner_frame(self.clock()), self._row_width()
        rows = [
            (t.id, task_label(t, n, frame, width=width))
            for t, n in zip(self.tasks, self.numbers, strict=True)
        ]
        self.show_rows(rows, Option(Text(NEW_TASK_LABEL, style="dim italic")))

    def _row_width(self) -> int:
        """The cells a row's line has, leaving room for a vertical scrollbar whether or not
        one shows, so a scrollbar coming or going never rewraps a row."""
        return self.content_region.width - self.styles.scrollbar_size_vertical


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
        models, _ = model_options(self._models())
        current = self._settings.model
        index = models.index(current) if current in models else None
        self._pick(self._model_picker, index, lambda i: self._update(model=models[i]))

    def _choose_tools(self) -> None:
        index = TOOL_ACCESS_OPTIONS.index(self._settings.tool_access)
        self._pick(
            self._tool_picker, index, lambda i: self._update(tool_access=TOOL_ACCESS_OPTIONS[i])
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
        mark_selected(self.query_one(f"#{FIELDS[self._field]}"), False)
        selected = self.query_one(f"#{FIELDS[field]}")
        mark_selected(selected, True)
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


class ReasonScreen(ModalScreen[None]):
    """Why task ``number`` ended, in full, as ``state`` had it when opened.

    j/k and the arrow keys scroll, y copies the text and escape closes.
    """

    AUTO_FOCUS = None
    DEFAULT_CSS = """
    ReasonScreen { align: center middle; }
    ReasonScreen > VerticalScroll {
        width: 100;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        border: round $accent;
        padding: 0 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j,down", "scroll(1)", show=False),
        Binding("k,up", "scroll(-1)", show=False),
        Binding("y", "copy", show=False),
        Binding("escape", "close", show=False),
    ]

    def __init__(self, state: TaskState, number: str) -> None:
        super().__init__()
        self.title_text = reason_title(state, number)
        self.text = reason_text(state)

    def compose(self) -> ComposeResult:
        with VerticalScroll(can_focus=False) as box:
            box.border_title = self.title_text
            box.border_subtitle = REASON_HINT
            yield Static(Text(self.text), id="reason-body")

    def action_scroll(self, step: int) -> None:
        self.query_one(VerticalScroll).scroll_relative(y=step, animate=False)

    def action_copy(self) -> None:
        self.app.copy_to_clipboard(self.text)
        self.app.notify(copied_note(self.text))

    def action_close(self) -> None:
        self.dismiss(None)
