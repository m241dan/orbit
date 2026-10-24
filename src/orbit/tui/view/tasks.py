"""The task list."""

from collections import Counter
from collections.abc import Callable, Sequence
from typing import Protocol

from rich.cells import cell_len
from rich.text import Text

from orbit.protocol import ACTIVE_STATUSES, Activity, TaskState, Usage
from orbit.protocol import edit_refusal as shared_edit_refusal
from orbit.tui.view.common import (
    CODER_WAIT,
    STATUS_ICONS,
    coder_wait_note,
    counted,
    spinner_frame,
    token_text,
)
from orbit.tui.view.detail_views import ERROR
from orbit.tui.view.settings import settings_fields

TASK_EDITOR_HINT = "j/k move · J/K scroll · i type · enter choose · ctrl+s save · esc cancel"
TASK_TITLE_TITLE = "Title"
TASK_PROMPT_TITLE = "Prompt"
TASK_TITLE_PLACEHOLDER = "Press i to type"
REASON_HINT = "j/k scroll · y copy · esc close"
NEW_TASK_LABEL = "+ new task"


def task_numbers(tasks: Sequence[TaskState]) -> list[str]:
    """Outline numbers: "1", "2", … for tasks, and "2.1", "2.2", … for the fixes under task 2.

    Every fix in a chain (a fix of a fix, too) is numbered under the task that started it. A
    fix whose task isn't on the list is numbered as a task.
    """
    numbers: list[str] = []
    top_number: dict[str, str] = {}  # top-level task id -> its number
    top_of: dict[str, str] = {}  # task id -> id of the top-level task it belongs to
    fixes_so_far: Counter[str] = Counter()
    for state in tasks:
        # A fix comes after the task it fixes, so that one is already placed if present.
        if state.fixes is None or state.fixes not in top_of:
            top = state.id
            top_number[top] = str(len(top_number) + 1)
            numbers.append(top_number[top])
        else:
            top = top_of[state.fixes]
            fixes_so_far[top] += 1
            numbers.append(f"{top_number[top]}.{fixes_so_far[top]}")
        top_of[state.id] = top
    return numbers


def task_label(
    state: TaskState,
    number: str,
    frame: str,
    *,
    width: int | None = None,
    waiting: str | None = None,
) -> Text:
    """A row: number, status and title, with the settings lined up under the title, and
    under them why the task ended, if it has a reason.

    Fix tasks are indented under the task they belong to. A running task shows the
    spinner ``frame``. A pending task about to run says it's ``waiting`` for a coder, if
    given. The reason's line is cut to ``width`` cells with an ellipsis, if given.
    """
    head = f"{number}. " if state.fixes is None else f"   {number} "
    icon = frame if state.status == "running" else STATUS_ICONS[state.status]
    # Long prompts (like fix tasks) are titled by their first line.
    title = split_prompt(state.task.prompt)[0]
    label = Text(f"{head}{icon} {title}\n")
    indent = " " * (len(head) + cell_len(icon) + 1)
    label.append(indent + _details(state, waiting), style="dim")
    if state.reason:
        label.append("\n")
        label.append_text(_reason_line(f"{indent}↳ {state.reason}", width))
    return label


def _details(state: TaskState, waiting: str | None) -> str:
    """What a row says under its title: the settings, the fix it's on, what it spent, and that
    it's ``waiting`` for a coder, if pending and given."""
    details = " · ".join(settings_fields(state.task.settings))
    if state.attempt:
        details += f" · fix {state.attempt}/{state.max_attempts}"
    if state.usage != Usage():
        details += f" · {usage_label(state.usage)}"
    if waiting is not None and state.status == "pending":
        details += f" · {waiting}"
    return details


def _reason_line(line: str, width: int | None) -> Text:
    """``line`` in dim red, cut to ``width`` cells with an ellipsis if given."""
    text = Text(line, style=f"dim {ERROR}")
    if width is not None:
        text.truncate(width, overflow="ellipsis")
    return text


def reason_title(state: TaskState, number: str) -> str:
    """The reason modal's title for task ``number``: why it stopped, if cancelled, else why it
    failed."""
    ending = "stopped" if state.status == "cancelled" else "failed"
    return f"Why task {number} {ending}"


def reason_text(state: TaskState) -> str:
    """Why ``state`` ended, in full: its reason, then a blank line and its detail, if it has
    one. It's what the reason modal shows and what y copies."""
    if not state.reason_detail:
        return state.reason
    return f"{state.reason}\n\n{state.reason_detail}"


def split_prompt(prompt: str) -> tuple[str, str]:
    """The first line of ``prompt`` as the title, and the rest as the body.

    The body loses the blank lines around it and the trailing whitespace of its last line, but
    keeps its indentation; a single line has an empty body, and an empty prompt gives ("", "").
    """
    title, _, body = prompt.partition("\n")
    return title, _trim_body(body)


def join_prompt(title: str, body: str) -> str:
    """The prompt the editor saves: ``title`` stripped, a blank line, then ``body`` trimmed.

    The body loses only the blank lines around it and the trailing whitespace of its last
    line; a blank body gives just the stripped title.
    """
    body = _trim_body(body)
    if not body:
        return title.strip()
    return f"{title.strip()}\n\n{body}"


def _trim_body(body: str) -> str:
    """``body`` without the blank lines around it or its last line's trailing whitespace."""
    lines = body.split("\n")
    while lines and not lines[0].strip():
        del lines[0]
    return "\n".join(lines).rstrip()


def coder_wait(activity: Sequence[Activity]) -> Activity | None:
    """The wait for a coder in ``activity``, if its task run waits for one."""
    return next((each for each in activity if each.kind == "waiting_for_coder"), None)


def coder_wait_row(activity: Sequence[Activity]) -> tuple[str, str] | None:
    """The task about to run while ``activity``'s task run waits for a coder, and what its row
    says: ``⏳ waiting: 3 coders busy``, or ``⏳ waiting for a coder`` while how many are busy
    isn't known; none while it doesn't wait or its task isn't known."""
    wait = coder_wait(activity)
    task_id = next((each.task_id for each in activity if each.kind == "task_run"), None)
    if wait is None or task_id is None:
        return None
    return task_id, _wait_note(wait.busy)


def _wait_note(busy: int | None) -> str:
    """What the row of the task waiting for a coder says while ``busy`` coders hold the slots:
    ``⏳ waiting: 3 coders busy``, or ``⏳ waiting for a coder`` while that isn't known."""
    note = CODER_WAIT if busy is None else coder_wait_note(f"{counted(busy, 'coder')} busy")
    return f"⏳ {note}"


def usage_label(usage: Usage) -> str:
    """Tokens and cost, shortly: ``1.2M tok · $0.84``."""
    return f"{token_text(usage.tokens)} tok · ${usage.cost_usd:.2f}"


def session_usage_subtitle(usage: Usage) -> str:
    """The task list's border subtitle: what the session has spent, ``usage``; empty when it
    has spent nothing."""
    return "" if usage == Usage() else f"session: {usage_label(usage)}"


def acceptable(state: TaskState) -> bool:
    """Whether ``state`` can be accepted: only a draft can."""
    return state.status == "draft"


def ticking(tasks: Sequence[TaskState]) -> bool:
    """Whether the spinners turn: while any of ``tasks`` runs."""
    return any(state.status == "running" for state in tasks)


def new_task_row() -> Text:
    """The task list's trailing row, to add a task."""
    return Text(NEW_TASK_LABEL, style="dim italic")


def active_task(tasks: Sequence[TaskState]) -> int | None:
    """The index of the topmost task that's running or verifying, in any role; None if none."""
    return next((i for i, t in enumerate(tasks) if t.status in ACTIVE_STATUSES), None)


def edit_refusal(state: TaskState) -> str | None:
    """Why ``state`` can't be edited, as the backend would refuse it; None if it can."""
    refusal = shared_edit_refusal(state)
    return None if refusal is None else refusal.message


def task_list_title(*, held: bool) -> str:
    """The task list's border title, which says when the list is held from following."""
    return "Task list · held" if held else "Task list"


def task_editor_title(*, new: bool) -> str:
    """The task editor's border title, for a ``new`` task or one being edited."""
    return "New task" if new else "Edit task"


def follow_top(
    rows: Sequence[tuple[int, int]], index: int, view_top: int, view_height: int
) -> int | None:
    """The scroll position that brings row ``index`` just into view, with the row above it
    too where there's room; None when the row is already in view.

    ``rows`` are each row's first line and height, and the view shows ``view_height`` lines
    from line ``view_top``.
    """
    top, height = rows[index]
    bottom = top + height
    if view_top <= top and bottom <= view_top + view_height:
        return None
    if top >= view_top:  # Below the view: bring its bottom up to the view's bottom.
        return min(bottom - view_height, top)
    context = rows[index - 1][0] if index else top
    return context if bottom - context <= view_height else top


class TaskBoardView(Protocol):
    """What the task list shows and what the user has done in it: the widget, which keeps the
    highlight and the scroll position as the user changes them, and is measured as it's laid
    out."""

    @property
    def highlighted(self) -> int | None:
        """The row highlighted, if any; the new-task row comes after the tasks'."""
        ...

    @property
    def row_width(self) -> int:
        """The cells a row's line has."""
        ...

    @property
    def scroll_top(self) -> int:
        """The first line in view."""
        ...

    @property
    def view_height(self) -> int:
        """How many lines are in view."""
        ...

    def row_lines(self) -> Sequence[tuple[int, int]]:
        """Each row's first line and height, once the list is measured; none before."""
        ...

    def show_title(self, title: str) -> None:
        """Show ``title`` over the list."""
        ...

    def show_usage(self, usage: str) -> None:
        """Show ``usage`` under the list, empty for none."""
        ...

    def show_labels(self, rows: Sequence[tuple[str, Text]], trailing: Text) -> None:
        """Show ``rows``, each a task's id and its label, then the row ``trailing``."""
        ...

    def highlight(self, index: int) -> None:
        """Highlight row ``index``."""
        ...

    def scroll_to_line(self, top: int) -> None:
        """Scroll at once so line ``top`` is the first in view."""
        ...

    def keep_ticking(self, running: bool) -> None:
        """Have :meth:`TaskBoard.redraw` called as fast as spinners turn while ``running``,
        and not otherwise."""
        ...

    def edit_task(self, task: TaskState | None) -> None:
        """Ask for ``task`` to be edited, or a new task for none."""
        ...

    def accept_task(self, task_id: str) -> None:
        """Ask for draft task ``task_id`` to be accepted."""
        ...

    def remove_task(self, task_id: str) -> None:
        """Ask for task ``task_id`` to be removed."""
        ...

    def show_why(self, state: TaskState, number: str) -> None:
        """Show why ``state``, task ``number`` on the list, ended."""
        ...


class TaskBoard:
    """The backend's task list as ``view`` shows it, a trailing row to add a task after its
    tasks, telling the time for its spinners with ``clock``.

    It numbers the tasks and labels their rows at the view's width, the task about to run
    saying when it waits for a coder, and redraws as the tasks change and while any runs. It
    totals what the session's turns cost. While following, it scrolls to keep the active task
    (``active_task``) in view; held, it stays where it is, until let go.
    """

    def __init__(self, view: TaskBoardView, clock: Callable[[], float]) -> None:
        self._view = view
        self._clock = clock
        self.tasks: tuple[TaskState, ...] = ()
        self.numbers: list[str] = []
        self.numbers_by_id: dict[str, str] = {}
        self.session_usage = Usage()
        self.waiting: tuple[str, str] | None = None  # The task waiting for a coder, and its note.
        self.following = True

    def start(self) -> None:
        """Show the list's title and its rows, following."""
        self._view.show_title(task_list_title(held=False))
        self.redraw()

    def show(self, tasks: Sequence[TaskState]) -> None:
        """Show ``tasks``, turning their spinners while any runs and following the active one;
        nothing for the tasks already shown."""
        tasks = tuple(tasks)
        if tasks == self.tasks:
            return
        self.tasks, self.numbers = tasks, task_numbers(tasks)
        self.numbers_by_id = dict(zip((t.id for t in tasks), self.numbers, strict=True))
        self.redraw()
        self._view.keep_ticking(ticking(tasks))
        self.follow()

    def show_waiting(self, waiting: tuple[str, str] | None) -> None:
        """Have the task ``waiting`` names say it waits for a coder as it says, or none do."""
        if waiting == self.waiting:
            return
        self.waiting = waiting
        self.redraw()

    def redraw(self) -> None:
        """Label the tasks' rows afresh, as the spinners are now and at the view's width."""
        frame, width = spinner_frame(self._clock()), self._view.row_width
        waiting_id, note = self.waiting or (None, None)
        rows = [
            (
                t.id,
                task_label(t, n, frame, width=width, waiting=note if t.id == waiting_id else None),
            )
            for t, n in zip(self.tasks, self.numbers, strict=True)
        ]
        self._view.show_labels(rows, new_task_row())

    def follow(self) -> None:
        """Scroll just enough to bring the active task into view, if following and the list
        is measured; the highlight stays where it is."""
        index = active_task(self.tasks)
        if not self.following or index is None:
            return
        rows = self._view.row_lines()
        if index >= len(rows):  # Not measured yet, before the list has a size.
            return
        top = follow_top(rows, index, self._view.scroll_top, self._view.view_height)
        if top is not None:
            self._view.scroll_to_line(top)

    def hold(self) -> None:
        """Stop following, so the list stays where the user put it."""
        self.following = False
        self._view.show_title(task_list_title(held=True))

    def let_go(self) -> None:
        """Follow the active task again, scrolling to it at once; nothing if not held."""
        if self.following:
            return
        self.following = True
        self._view.show_title(task_list_title(held=False))
        self.follow()

    def activated(self, focused: bool) -> None:
        """Highlight the active task, if there is one, now the list is activated, if it took
        the focus (``focused``), as a disabled list doesn't."""
        index = active_task(self.tasks)
        if focused and index is not None:
            self._view.highlight(index)

    @property
    def highlighted_task(self) -> TaskState | None:
        """The task the highlight is on; none on the new-task row or with nothing highlighted."""
        index = self._view.highlighted
        return self.tasks[index] if index is not None and index < len(self.tasks) else None

    def select(self) -> None:
        """Ask to edit the task highlighted, or a new task on the new-task row; nothing with
        nothing highlighted."""
        if self._view.highlighted is not None:
            self._view.edit_task(self.highlighted_task)

    def accept(self) -> None:
        """Ask to accept the task highlighted, if it's a draft."""
        state = self.highlighted_task
        if state is not None and acceptable(state):
            self._view.accept_task(state.id)

    def remove(self) -> None:
        """Ask to remove the task highlighted, if any."""
        state = self.highlighted_task
        if state is not None:
            self._view.remove_task(state.id)

    def explain(self) -> None:
        """Show why the task highlighted ended, if it has a reason."""
        state = self.highlighted_task
        if state is not None and state.reason:
            self._view.show_why(state, self.numbers_by_id[state.id])

    def count(self, usage: Usage) -> None:
        """Add what a turn cost, ``usage``, to the session's total."""
        self.reset_usage(self.session_usage + usage)

    def reset_usage(self, usage: Usage) -> None:
        """Start the session's total afresh at ``usage``; a session that has spent nothing
        shows no total."""
        self.session_usage = usage
        self._view.show_usage(session_usage_subtitle(usage))

    def find(self, task_id: str) -> TaskState | None:
        """The task with id ``task_id``, if it's on the list."""
        return next((t for t in self.tasks if t.id == task_id), None)
