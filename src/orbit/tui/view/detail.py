"""The detail pane: the full content of a tool call or request to orbit."""

import json
from collections.abc import Callable

from rich.console import Group, RenderableType
from rich.text import Text

from orbit.tui.view.common import counted, text_lines
from orbit.tui.view.diffs import file_diff
from orbit.tui.view.tool_calls import ToolCall

DETAIL_HEIGHT = 40
"""How much of the conversation's height the detail pane takes, in percent."""
DETAIL_COVER_BELOW = 20
"""The conversation's height, in rows, below which the open detail pane covers all of it."""
DETAIL_LINE_LIMIT = 200
"""The most lines of output the detail pane shows; longer output keeps its two ends."""
DETAIL_TITLE = "Detail"
COPY_HINT = "y copy"
COPY_ALL_HINT = "y copies all"
NO_DETAILS = "No details for this entry"
WAITING = "waiting for output…"
EMPTY_DIFF = "no lines changed"
DIM = "dim"


def detail_covers(height: int) -> bool:
    """Whether the open detail pane covers the whole conversation, ``height`` rows high."""
    return height < DETAIL_COVER_BELOW


def detail_title(call: ToolCall | None) -> str:
    """The detail pane's title: ``call``'s summary and a hint that y copies it, if there's a
    call."""
    return DETAIL_TITLE if call is None else f"{DETAIL_TITLE} · {call.summary} · {COPY_HINT}"


def detail_copy(call: ToolCall | None) -> str | None:
    """What y copies from the detail pane for ``call``: the whole diff an edit makes, if it has
    lines, else its whole output or reply, or its arguments as JSON while it runs or when it has
    no output; None when there's no call."""
    if call is None:
        return None
    diff = _diff_copy(call)
    if diff:
        return diff
    if call.status in ("running", "no_result") or not call.output:
        return json.dumps(call.arguments, indent=2, ensure_ascii=False)
    return call.output


def _diff_copy(call: ToolCall) -> str:
    """The whole diff ``call`` makes, if it's an edit; empty otherwise."""
    diff = None if call.request else file_diff(call.name, call.arguments)
    return "" if diff is None else "\n".join(diff.lines)


def copied_note(text: str) -> str:
    """The note that ``text`` was copied, counting its lines."""
    return f"Copied {counted(len(text_lines(text)), 'line')}"


def capped_with(
    output: str, render: Callable[[str], RenderableType], limit: int = DETAIL_LINE_LIMIT
) -> RenderableType:
    """``output`` drawn by ``render``; with more than ``limit`` lines, just its first and last
    halves of that, each drawn on its own, with a dim line between them counting the lines left
    out and saying y copies them all."""
    lines = text_lines(output)
    if len(lines) <= limit:
        return render(output)
    kept = limit // 2
    head, tail = "\n".join(lines[:kept]), "\n".join(lines[-kept:])
    return Group(render(head), _omitted(len(lines) - 2 * kept), render(tail))


def _omitted(count: int) -> Text:
    return Text(f"… {counted(count, 'line')} omitted · {COPY_ALL_HINT} …", style=DIM)
