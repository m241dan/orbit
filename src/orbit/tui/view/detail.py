"""The detail pane: the full content of a tool call or request to orbit."""

import json
from collections.abc import Mapping
from typing import Any

from rich.console import Group, RenderableType
from rich.rule import Rule
from rich.text import Text

from orbit.tui.view.tool_calls import ToolCall

DETAIL_HEIGHT = 40
"""How much of the conversation's height the detail pane takes, in percent."""
DETAIL_LINE_LIMIT = 200
"""The most lines of output the detail pane shows; longer output keeps its two ends."""
DETAIL_TITLE = "Detail"
NO_DETAILS = "No details for this entry"
WAITING = "waiting for output…"
DIM = "dim"


def detail_title(call: ToolCall | None) -> str:
    """The detail pane's title: ``call``'s summary, if there's a call."""
    return DETAIL_TITLE if call is None else f"{DETAIL_TITLE} · {call.summary}"


def detail_body(call: ToolCall | None) -> RenderableType:
    """What the detail pane shows for ``call``: its arguments, a rule, then its output or reply,
    or a dim note that it's still waiting; a dim note when there's no call."""
    if call is None:
        return Text(NO_DETAILS, style=DIM)
    output = Text(WAITING, style=DIM) if call.status == "running" else capped(call.output)
    return Group(argument_lines(call.arguments), Rule(style=DIM), output)


def argument_lines(arguments: Mapping[str, Any]) -> Text:
    """``arguments`` as ``key: value`` lines, text kept as it is, with its newlines, and
    anything else as JSON."""
    return Text("\n".join(f"{key}: {_shown(value)}" for key, value in arguments.items()))


def _shown(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def capped(output: str, limit: int = DETAIL_LINE_LIMIT) -> Text:
    """``output`` as plain text; with more than ``limit`` lines, just its first and last
    halves of that, a dim line between them counting the lines left out."""
    lines = output.splitlines()
    if len(lines) <= limit:
        return Text(output)
    kept = limit // 2
    omitted = len(lines) - 2 * kept
    marker = Text(f"… {omitted} line{'' if omitted == 1 else 's'} omitted …", style=DIM)
    return Text("\n").join([Text("\n".join(lines[:kept])), marker, Text("\n".join(lines[-kept:]))])
