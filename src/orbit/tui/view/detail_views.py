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

from orbit.tui.view.detail import DIM, EMPTY_DIFF, NO_DETAILS, WAITING, capped_with, output_lines
from orbit.tui.view.diff_numbers import NUMBERED_LINE, diff_numbers, numbered_diff
from orbit.tui.view.diffs import FileDiff, file_diff
from orbit.tui.view.tool_calls import ToolCall

SYNTAX_THEME = "ansi_dark"
"""The highlighting theme, drawn in the terminal's own colours."""
READ_SPAN = ("offset", "limit")
HEADER = "bold"
ERROR = "red"
DIFF_STYLES: Mapping[str, str] = MappingProxyType({"-": "red", "+": "green"})
"""How a diff's removed and added lines are drawn; the rest are dim."""

type Render = Callable[[str], RenderableType]
type View = Callable[[ToolCall], RenderableType | None]


def detail_body(call: ToolCall | None) -> RenderableType:
    """What the detail pane shows for ``call``: a header in the form that suits its tool, a
    rule, then its output or orbit's reply, or a dim note that it's still waiting; a dim note
    when there's no call. A call whose arguments don't suit its tool's view shows them as
    JSON, or nothing when it has none, so its body begins with the rule."""
    if call is None:
        return Text(NO_DETAILS, style=DIM)
    view = _request_view if call.request else TOOL_VIEWS.get(call.name, _generic_view)
    body = view(call)
    return _generic_view(call) if body is None else body


def _shown(header: RenderableType | None, call: ToolCall, render: Render) -> Group:
    """``header``, if there is one, a rule, then ``call``'s output capped and drawn by
    ``render``, or a dim note while it runs."""
    waiting = call.status == "running"
    output = Text(WAITING, style=DIM) if waiting else capped_with(call.output, render)
    rule = Rule(style=DIM)
    return Group(rule, output) if header is None else Group(header, rule, output)


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
    lines = output_lines(output)
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
        match = NUMBERED_LINE.fullmatch(line)
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


def _diff_view(call: ToolCall) -> RenderableType | None:
    """The file's path, the diff ``call`` makes to it, its lines numbered where that's sure, or
    a dim note when it has none, then its output, dim, or red if it failed."""
    diff = file_diff(call.name, call.arguments)
    if diff is None:
        return None
    header = Group(Text(diff.path, style=HEADER), Rule(style=DIM), _diff_lines(call, diff))
    style = ERROR if call.status == "failed" else DIM
    return _shown(header, call, lambda text: Text(text, style=style))


def _diff_lines(call: ToolCall, diff: FileDiff) -> RenderableType:
    """``diff``'s lines, numbered as ``call``'s output shows them and capped, or a dim note
    when it has none."""
    if not diff.lines:
        return Text(EMPTY_DIFF, style=DIM)
    numbered, gutter = numbered_diff(diff.lines, _diff_numbers(call, diff))
    return capped_with("\n".join(numbered), partial(diff_text, gutter=gutter))


def _diff_numbers(call: ToolCall, diff: FileDiff) -> list[int | None]:
    """The line numbers of ``diff``'s lines, as ``call``'s output shows them; none if it
    failed."""
    if call.status == "failed":
        return [None] * len(diff.lines)
    return diff_numbers(call.name, diff.lines, call.output)


def diff_text(diff: str, gutter: int = 0) -> Text:
    """The lines of ``diff``, each after a dim column ``gutter`` wide: removed ones red, added
    ones green and the rest dim."""
    return Text("\n").join(_diff_line(line, gutter) for line in diff.splitlines())


def _diff_line(line: str, gutter: int) -> Text:
    body = line[gutter:]
    return Text.assemble((line[:gutter], DIM), (body, DIFF_STYLES.get(body[:1], DIM)))


def _generic_view(call: ToolCall) -> RenderableType:
    """``call``'s arguments as JSON, if it has any, then its output, as JSON too if it is a
    JSON object or array."""
    pretty = pretty_json(call.output)
    if pretty is None:
        return _shown(_arguments(call), call, Text)
    return _shown(_arguments(call), replace(call, output=pretty), _json_syntax)


def pretty_json(text: str) -> str | None:
    """``text`` indented, if it's a JSON object or array, else None."""
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return _dumped(value) if isinstance(value, dict | list) else None


def _dumped(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _arguments(call: ToolCall) -> Syntax | None:
    """``call``'s arguments as JSON; None when it has none, so its body begins with the
    rule."""
    return _json_syntax(_dumped(call.arguments)) if call.arguments else None


def _json_syntax(text: str) -> Syntax:
    return _syntax(text, "json")


def _request_view(call: ToolCall) -> RenderableType:
    """A request's arguments as JSON, if it has any, then orbit's reply, red if it's an
    error."""
    style = ERROR if call.status == "failed" else ""
    reply = replace(call, output=f"orbit: {call.output}")
    return _shown(_arguments(call), reply, lambda text: Text(text, style=style))


TOOL_VIEWS: Mapping[str, View] = MappingProxyType(
    {
        "Read": _read_view,
        "Bash": _bash_view,
        "Grep": _search_view,
        "Glob": _search_view,
        "Edit": _diff_view,
        "MultiEdit": _diff_view,
        "Write": _diff_view,
    }
)
"""How the detail pane shows the calls of the tools that have a view of their own."""
