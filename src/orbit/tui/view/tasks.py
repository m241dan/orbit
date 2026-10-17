"""The task list."""

from collections import Counter
from collections.abc import Sequence

from rich.cells import cell_len
from rich.text import Text

from orbit.protocol import ACTIVE_STATUSES, TaskState, Usage
from orbit.tui.view.common import STATUS_ICONS, model_label, token_text
from orbit.tui.view.detail_views import ERROR

TASK_EDITOR_HINT = "j/k move · J/K scroll · i type · enter choose · ctrl+s save · esc cancel"


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


def task_label(state: TaskState, number: str, frame: str, *, width: int | None = None) -> Text:
    """A row: number, status and title, with the settings lined up under the title, and
    under them why the task ended, if it has a reason.

    Fix tasks are indented under the task they belong to. A running task shows the
    spinner ``frame``. The reason's line is cut to ``width`` cells with an ellipsis, if given.
    """
    settings = state.task.settings
    head = f"{number}. " if state.fixes is None else f"   {number} "
    icon = frame if state.status == "running" else STATUS_ICONS[state.status]
    # Long prompts (like fix tasks) are titled by their first line.
    title = split_prompt(state.task.prompt)[0]
    label = Text(f"{head}{icon} {title}\n")
    indent = " " * (len(head) + cell_len(icon) + 1)
    details = f"{indent}{model_label(settings)} · {settings.tool_access}"
    if state.attempt:
        details += f" · fix {state.attempt}/{state.max_attempts}"
    if state.usage != Usage():
        details += f" · {usage_label(state.usage)}"
    label.append(details, style="dim")
    if state.reason:
        label.append("\n")
        label.append_text(_reason_line(f"{indent}↳ {state.reason}", width))
    return label


def _reason_line(line: str, width: int | None) -> Text:
    """``line`` in dim red, cut to ``width`` cells with an ellipsis if given."""
    text = Text(line, style=f"dim {ERROR}")
    if width is not None:
        text.truncate(width, overflow="ellipsis")
    return text


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


def usage_label(usage: Usage) -> str:
    """Tokens and cost, shortly: ``1.2M tok · $0.84``."""
    return f"{token_text(usage.tokens)} tok · ${usage.cost_usd:.2f}"


def active_task(tasks: Sequence[TaskState]) -> int | None:
    """The index of the topmost task that's running or verifying, in any role; None if none."""
    return next((i for i, t in enumerate(tasks) if t.status in ACTIVE_STATUSES), None)


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
