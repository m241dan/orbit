"""The task list."""

from collections import Counter
from collections.abc import Sequence

from rich.cells import cell_len
from rich.text import Text

from orbit.protocol import TaskState, Usage
from orbit.tui.view.common import STATUS_ICONS, model_label, token_text


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


def task_label(state: TaskState, number: str, frame: str) -> Text:
    """A row: number, status and title, with the settings lined up under the title.

    Fix tasks are indented under the task they belong to. A running task shows the
    spinner ``frame``.
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
    return label


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
