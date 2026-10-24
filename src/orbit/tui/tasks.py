"""The task list window."""

import time
from collections.abc import Callable, Sequence
from typing import ClassVar

from rich.text import Text
from textual.binding import Binding, BindingType
from textual.message import Message
from textual.widgets.option_list import Option

from orbit.protocol import TaskState
from orbit.tui.list_pane import ListPane
from orbit.tui.view.tasks import TaskBoard
from orbit.tui.widgets import Ticker


class TaskList(ListPane):
    """Shows the :class:`TaskBoard`: the backend's task list plus a trailing "new task" row.

    Like the prompt, it only takes focus once activated (``enter_list_mode``);
    then j/k move, enter edits a row, ``a`` accepts a draft, ``x`` removes a task, ``f`` shows
    why a task ended and escape returns to window navigation. Its border shows what the
    session's turns cost, and the task about to run says when it waits for a coder. While
    following, it scrolls to keep the active task in view; activating it or scrolling it with
    the mouse wheel holds it where it is, until the board lets go of it.
    """

    class Accept(Message):
        """The user accepted draft task ``task_id``."""

        def __init__(self, task_id: str) -> None:
            super().__init__()
            self.task_id = task_id

    class Edit(Message):
        """The user picked ``task`` to edit, or ``None`` for the new-task row."""

        def __init__(self, task: TaskState | None) -> None:
            super().__init__()
            self.task = task

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
        self.clock: Callable[[], float] = time.monotonic
        self.board = TaskBoard(self, lambda: self.clock())
        self._ticker = Ticker(self, self.tick)

    @property
    def tasks(self) -> tuple[TaskState, ...]:
        """The tasks shown, in order."""
        return self.board.tasks

    @property
    def following(self) -> bool:
        """Whether the list follows the active task."""
        return self.board.following

    def on_mount(self) -> None:
        self.board.start()

    def on_resize(self) -> None:
        # Reasons are cut to the list's width, so a new width relabels their rows. OptionList
        # re-measures its rows in its own resize handler, which runs after this one.
        self.fit()

    def fit(self) -> None:
        """Relabel the rows at the list's width and follow the active task, as on a resize;
        for a list shown again, which Textual doesn't resize when its size is unchanged."""
        self.board.redraw()
        self.call_after_refresh(self.board.follow)

    def enter_list_mode(self) -> None:
        """Activate and hold the list, with the highlight on the active task if there is one."""
        self.board.hold()
        super().enter_list_mode()
        self.board.activated(self.can_focus)

    def on_mouse_scroll_down(self) -> None:
        self.board.hold()

    def on_mouse_scroll_up(self) -> None:
        self.board.hold()

    def action_select(self) -> None:
        # Resolved now, on enter or a click, rather than from a queued OptionSelected, so a list
        # replaced before that message is handled can't change which task is edited.
        self.board.select()

    def action_accept(self) -> None:
        self.board.accept()

    def action_remove(self) -> None:
        self.board.remove()

    def action_show_reason(self) -> None:
        self.board.explain()

    def show(self, tasks: Sequence[TaskState]) -> None:
        """Show ``tasks``; the highlight stays on its task, or on the new-task row.

        A task that changed has its row relabelled in place; only tasks coming, going or
        moving rebuild the rows.
        """
        self.board.show(tasks)

    @property
    def ticking(self) -> bool:
        """Whether the spinners of running tasks are turning."""
        return self._ticker.ticking

    def tick(self) -> None:
        """Turn the spinners: redraw just the rows of running tasks."""
        self.board.redraw()

    @property
    def row_width(self) -> int:
        """The cells a row's line has, leaving room for a vertical scrollbar whether or not
        one shows, so a scrollbar coming or going never rewraps a row."""
        return self.content_region.width - self.styles.scrollbar_size_vertical

    @property
    def scroll_top(self) -> int:
        return round(self.scroll_y)

    @property
    def view_height(self) -> int:
        return self.scrollable_content_region.height

    def show_title(self, title: str) -> None:
        self.border_title = title

    def show_usage(self, usage: str) -> None:
        self.border_subtitle = usage

    def show_labels(self, rows: Sequence[tuple[str, Text]], trailing: Text) -> None:
        self.show_rows(rows, Option(trailing))

    def highlight(self, index: int) -> None:
        self.highlighted = index

    def scroll_to_line(self, top: int) -> None:
        self.scroll_to(y=top, animate=False, immediate=True)

    def keep_ticking(self, running: bool) -> None:
        self._ticker.keep(running)

    def edit_task(self, task: TaskState | None) -> None:
        self.post_message(self.Edit(task))

    def accept_task(self, task_id: str) -> None:
        self.post_message(self.Accept(task_id))

    def remove_task(self, task_id: str) -> None:
        self.post_message(self.Remove(task_id))

    def show_why(self, state: TaskState, number: str) -> None:
        self.post_message(self.ShowReason(state, number))
