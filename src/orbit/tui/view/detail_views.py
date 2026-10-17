"""What the detail pane shows for each tool: its arguments in the form that suits it, a rule,
then its output, capped before it's drawn."""

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from functools import partial
from types import MappingProxyType
from typing import Any

from rich.console import Group, RenderableType
from rich.rule import Rule
from rich.syntax import Syntax
from rich.text import Text

from orbit.tui.view.detail import DIM, NO_DETAILS, WAITING, capped_with
from orbit.tui.view.tool_calls import ToolCall

SYNTAX_THEME = "ansi_dark"
"""The highlighting theme, drawn in the terminal's own colours."""
READ_LINE = re.compile(r" *(\d+)(?:→|\t)(.*)")
"""A line of Claude Code's Read output: its line number, an arrow or a tab, then the line."""
READ_SPAN = ("offset", "limit")
HEADER = "bold"
ERROR = "red"

type Render = Callable[[str], RenderableType]
type View = Callable[[ToolCall], RenderableType | None]


def detail_body(call: ToolCall | None) -> RenderableType:
    """What the detail pane shows for ``call``: a header in the form that suits its tool, a
    rule, then its output or orbit's reply, or a dim note that it's still waiting; a dim note
    when there's no call. A call whose arguments don't suit its tool's view shows them as
    JSON."""
    if call is None:
        return Text(NO_DETAILS, style=DIM)
    view = _request_view if call.request else TOOL_VIEWS.get(call.name, _generic_view)
    body = view(call)
    return _generic_view(call) if body is None else body


def _shown(header: RenderableType, call: ToolCall, render: Render) -> Group:
    """``header``, a rule, then ``call``'s output capped and drawn by ``render``, or a dim note
    while it runs."""
    waiting = call.status == "running"
    output = Text(WAITING, style=DIM) if waiting else capped_with(call.output, render)
    return Group(header, Rule(style=DIM), output)


def _string(arguments: Mapping[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    return value if isinstance(value, str) else None


def _read_view(call: ToolCall) -> RenderableType | None:
    path = _string(call.arguments, "file_path")
    if path is None:
        return None
    header = Text.assemble((path, HEADER))
    span = [f"{key} {call.arguments[key]}" for key in READ_SPAN if key in call.arguments]
    if span:
        header.append(f" ({', '.join(span)})", style=DIM)
    return _shown(header, call, partial(read_output, path))


def read_output(path: str, output: str) -> RenderableType:
    """Read's ``output`` of the file at ``path``: its numbered lines highlighted for the file's
    type, numbers kept, then any lines after them as plain text; all plain without numbers."""
    lines = output.splitlines()
    numbered = _numbered(lines)
    if not numbered:
        return Text(output)
    code = "\n".join(match[2] for match in numbered)
    syntax = _syntax(code, Syntax.guess_lexer(path, code), start_line=int(numbered[0][1]))
    rest = lines[len(numbered) :]
    return Group(syntax, Text("\n".join(rest))) if rest else syntax


def _numbered(lines: Iterable[str]) -> list[re.Match[str]]:
    """The numbered lines that ``lines`` start with."""
    matches = []
    for line in lines:
        match = READ_LINE.fullmatch(line)
        if match is None:
            break
        matches.append(match)
    return matches


def _syntax(code: str, lexer: str, start_line: int | None = None) -> Syntax:
    """``code`` highlighted by ``lexer``, numbered from ``start_line`` if it's given."""
    return Syntax(
        code,
        lexer,
        theme=SYNTAX_THEME,
        background_color="default",
        line_numbers=start_line is not None,
        start_line=start_line or 1,
        word_wrap=True,
    )


def _bash_view(call: ToolCall) -> RenderableType | None:
    command = _string(call.arguments, "command")
    if command is None:
        return None
    return _shown(Text(f"$ {command}", style=HEADER), call, Text.from_ansi)


def _search_view(call: ToolCall) -> RenderableType | None:
    pattern = _string(call.arguments, "pattern")
    if pattern is None:
        return None
    header = Text.assemble((pattern, HEADER))
    path = _string(call.arguments, "path")
    if path is not None:
        header.append(f" in {path}")
    return _shown(header, call, Text)


def _generic_view(call: ToolCall) -> RenderableType:
    """``call``'s arguments as JSON, then its output, as JSON too if it is a JSON object or
    array."""
    pretty = pretty_json(call.output)
    if pretty is None:
        return _shown(_json(call.arguments), call, Text)
    return _shown(_json(call.arguments), replace(call, output=pretty), _json_syntax)


def pretty_json(text: str) -> str | None:
    """``text`` indented, if it's a JSON object or array, else None."""
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return _dumped(value) if isinstance(value, dict | list) else None


def _dumped(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _json(value: object) -> Syntax:
    return _json_syntax(_dumped(value))


def _json_syntax(text: str) -> Syntax:
    return _syntax(text, "json")


def _request_view(call: ToolCall) -> RenderableType:
    """A request's arguments as JSON, then orbit's reply, red if it's an error."""
    style = ERROR if call.status == "failed" else ""
    reply = replace(call, output=f"orbit: {call.output}")
    return _shown(_json(call.arguments), reply, lambda text: Text(text, style=style))


TOOL_VIEWS: Mapping[str, View] = MappingProxyType(
    {
        "Read": _read_view,
        "Bash": _bash_view,
        "Grep": _search_view,
        "Glob": _search_view,
    }
)
"""How the detail pane shows the calls of the tools that have a view of their own."""
