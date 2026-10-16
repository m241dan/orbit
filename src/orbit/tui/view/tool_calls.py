"""Tool calls and agents' requests to orbit, summed up in a line."""

import json
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

TOOL_CALL_SUMMARY_LIMIT = 120
"""The most characters a tool call's summary line shows."""
ORBIT_TOOL_PREFIX = "mcp__orbit__"
ORBIT_TOOL_FIELDS = ("summary", "title", "note", "reason", "name")
"""The arguments of orbit's own tools that sum up a call, best first."""

type ArgumentExtractor = Callable[[Mapping[str, Any]], str | None]


def _text(arguments: Mapping[str, Any], key: str) -> str | None:
    """The string at ``key`` in ``arguments``, or None when it's missing, not text or blank."""
    value = arguments.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _field(key: str) -> ArgumentExtractor:
    return lambda arguments: _text(arguments, key)


def _first_line(arguments: Mapping[str, Any]) -> str | None:
    command = _text(arguments, "command")
    return None if command is None else command.lstrip().partition("\n")[0]


def _searched(arguments: Mapping[str, Any]) -> str | None:
    pattern = _text(arguments, "pattern")
    path = _text(arguments, "path")
    return pattern if pattern is None or path is None else f"{pattern} in {path}"


def _todos(arguments: Mapping[str, Any]) -> str | None:
    todos = arguments.get("todos")
    return f"{len(todos)} todos" if isinstance(todos, list) else None


KEY_ARGUMENTS: Mapping[str, ArgumentExtractor] = MappingProxyType(
    {
        "Read": _field("file_path"),
        "Write": _field("file_path"),
        "Edit": _field("file_path"),
        "MultiEdit": _field("file_path"),
        "NotebookEdit": _field("notebook_path"),
        "Bash": _first_line,
        "Grep": _searched,
        "Glob": _searched,
        "WebSearch": _field("query"),
        "WebFetch": _field("url"),
        "Task": _field("description"),
        "Agent": _field("description"),
        "TodoWrite": _todos,
    }
)
"""How to find the argument that sums up a call to each of Claude Code's built-in tools."""


def tool_call_summary(name: str, arguments: Mapping[str, Any]) -> str:
    """The tool ``name`` and the argument that sums up its call, clipped to one short line.

    A tool with no such argument among ``arguments`` shows them all, as compact JSON.
    """
    if name.startswith(ORBIT_TOOL_PREFIX):
        summary = _orbit_tool_summary(name.removeprefix(ORBIT_TOOL_PREFIX), arguments)
    else:
        summary = _built_in_tool_summary(name, arguments)
    return _clip(summary, TOOL_CALL_SUMMARY_LIMIT)


def _orbit_tool_summary(tool: str, arguments: Mapping[str, Any]) -> str:
    texts = (_text(arguments, key) for key in ORBIT_TOOL_FIELDS)
    text = next((text for text in texts if text is not None), None)
    return tool if text is None else f"{tool} {_squeezed(text)}"


def _built_in_tool_summary(name: str, arguments: Mapping[str, Any]) -> str:
    extract = KEY_ARGUMENTS.get(name)
    key = None if extract is None else extract(arguments)
    if key is None:
        return f"{name} {json.dumps(arguments, ensure_ascii=False)}"
    return f"{name} {_squeezed(key)}"


def _squeezed(text: str) -> str:
    """``text`` with each run of whitespace, newlines included, as a single space."""
    return " ".join(text.split())


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def tool_call_text(name: str, arguments: Mapping[str, Any]) -> str:
    """A tool call as one line summing it up, with its full ``arguments`` on the lines after."""
    if not arguments:
        return f"→ {name}"
    full = json.dumps(arguments, indent=2, ensure_ascii=False)
    return f"→ {tool_call_summary(name, arguments)}\n{full}"


def orbit_request_text(tool: str, arguments: Mapping[str, Any]) -> str:
    """A request to orbit's ``tool`` as one line summing it up, with its full ``arguments`` on
    the lines after."""
    if not arguments:
        return tool
    summary = _clip(_orbit_tool_summary(tool, arguments), TOOL_CALL_SUMMARY_LIMIT)
    return f"{summary}\n{json.dumps(arguments, indent=2, ensure_ascii=False)}"
