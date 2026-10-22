"""The task list window."""

import time
from collections.abc import Callable, Sequence
from typing import ClassVar

from textual import events
from textual.binding import Binding, BindingType
from textual.message import Message
from textual.widgets.option_list import Option

from orbit.protocol import TaskState, Usage
from orbit.tui.list_pane import ListPane
from orbit.tui.view.common import spinner_frame
from orbit.tui.view.tasks import (
    acceptable,
    active_task,
    follow_top,
    new_task_row,
    session_usage_subtitle,
    task_label,
    task_list_title,
    task_numbers,
    ticking,
)
from orbit.tui.widgets import Ticker


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
        self.fit()

    def fit(self) -> None:
        """Relabel the rows at the list's width and follow the active task, as on a resize;
        for a list shown again, which Textual doesn't resize when its size is unchanged."""
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
        if state is not None and acceptable(state):
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
        self._ticker.keep(ticking(tasks))
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
        self.show_rows(rows, Option(new_task_row()))

    def _row_width(self) -> int:
        """The cells a row's line has, leaving room for a vertical scrollbar whether or not
        one shows, so a scrollbar coming or going never rewraps a row."""
        return self.content_region.width - self.styles.scrollbar_size_vertical
