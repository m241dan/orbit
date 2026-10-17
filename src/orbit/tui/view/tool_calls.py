"""Tool calls and agents' requests to orbit, summed up in a line."""

import difflib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Literal, assert_never

from orbit.protocol import AgentReply, AgentRequest, ToolCallFinished, ToolCallStarted
from orbit.tui.view.common import EntryKind, clipped, counted, text_lines

TOOL_CALL_SUMMARY_LIMIT = 120
"""The most cells a tool call's summary line takes."""
REPLY_LIMIT = 60
"""The most cells of orbit's reply a request's line takes."""
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
    return clipped(summary, TOOL_CALL_SUMMARY_LIMIT)


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


def tool_call_title(name: str, arguments: Mapping[str, Any]) -> str:
    """The line summing up a call to the tool ``name``: the tool alone without ``arguments``,
    else its summary (see :func:`tool_call_summary`)."""
    return tool_call_summary(name, arguments) if arguments else name


ToolCallStatus = Literal["running", "passed", "failed", "no_result"]
"""How a tool call went: still running, finished or failed, or ended before a result came."""


@dataclass(frozen=True)
class ToolCall:
    """A tool call as its entry shows it: the ``call_id`` its result comes back with, the tool's
    ``name``, its ``summary`` and ``status``, and its full ``arguments`` and ``output``, kept for
    a closer look. A ``request`` is an agent's call to orbit, its output orbit's reply."""

    call_id: str
    name: str
    summary: str
    arguments: Mapping[str, Any]
    status: ToolCallStatus = "running"
    output: str = ""
    request: bool = False


def tool_call_started(event: ToolCallStarted | AgentRequest) -> ToolCall:
    """The call ``event`` starts, running: a tool call, or an agent's request to orbit."""
    if isinstance(event, AgentRequest):
        summary = clipped(_orbit_tool_summary(event.tool, event.arguments), TOOL_CALL_SUMMARY_LIMIT)
        return ToolCall(event.request_id, event.tool, summary, event.arguments, request=True)
    title = tool_call_title(event.name, event.arguments)
    return ToolCall(event.call_id, event.name, title, event.arguments)


def finishes(event: ToolCallFinished | AgentReply, call: ToolCall, agent: str | None) -> bool:
    """Whether ``event`` is the result of ``agent``'s ``call``: a tool's output for a tool call,
    orbit's reply for a request."""
    if isinstance(event, AgentReply):
        return call.request and (agent, call.call_id) == (event.agent_id, event.request_id)
    return not call.request and (agent, call.call_id) == (event.agent_id, event.call_id)


def tool_call_finished(call: ToolCall, event: ToolCallFinished | AgentReply) -> ToolCall:
    """``call`` finished with ``event``'s output or reply, failed if it's an error."""
    output = event.text if isinstance(event, AgentReply) else event.output
    return replace(call, status="failed" if event.is_error else "passed", output=output)


def tool_call_unfinished(call: ToolCall) -> ToolCall:
    """``call`` once its turn, or its agent, has ended: with no result if it was still
    running."""
    return replace(call, status="no_result") if call.status == "running" else call


def line_count(text: str) -> str:
    """How many lines ``text`` has, as read: ``0 lines``, ``1 line``, ``2 lines``."""
    return counted(len(text_lines(text)), "line")


NO_MATCHES = frozenset({"No matches found", "No files found"})
"""What Claude Code's Grep and Glob say when they find nothing."""
SEARCH_TALLY = re.compile(r"Found \d+ ")
"""The start of the line where Claude Code's Grep tallies its files or occurrences."""
SEARCH_TRUNCATED = re.compile(
    r"\((?:Results are truncated\. |Showing \d+ of \d+ matching files; "
    r"|Showing the first \d+ files; )"
)
"""The start of the line where Claude Code's Glob notes that it listed only some files."""
SEARCH_PAGINATION = re.compile(r"\[Showing results with pagination = |No entries at this offset\b")
"""The start of the line where Claude Code's Grep notes how it paged its results, or that the
page it was asked for is empty."""
EXIT_CODE = re.compile(r"^Exit code (\d+)\b", re.MULTILINE)
"""The line where Claude Code says what a failed command exited with."""

type ResultSummarizer = Callable[[Mapping[str, Any], str], str | None]


def _line_change(old: str, new: str) -> tuple[int, int]:
    """How many lines turning ``old`` into ``new`` adds and removes, unchanged lines aside."""
    added = removed = 0
    matcher = difflib.SequenceMatcher(None, text_lines(old), text_lines(new), autojunk=False)
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag != "equal":
            added += new_end - new_start
            removed += old_end - old_start
    return added, removed


def _edit_change(edit: object) -> tuple[int, int] | None:
    """The lines ``edit`` adds and removes, or None unless it has both strings."""
    if not isinstance(edit, Mapping):
        return None
    old, new = edit.get("old_string"), edit.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str):
        return None
    return _line_change(old, new)


def _changed(change: tuple[int, int] | None) -> str | None:
    return None if change is None else f"✓ +{change[0]} \N{MINUS SIGN}{change[1]}"


def _edited(arguments: Mapping[str, Any], output: str) -> str | None:
    return _changed(_edit_change(arguments))


def _multi_edited(arguments: Mapping[str, Any], output: str) -> str | None:
    edits = arguments.get("edits")
    changes = [_edit_change(edit) for edit in edits] if isinstance(edits, list) else [None]
    return _changed(_total(changes))


def _total(changes: Iterable[tuple[int, int] | None]) -> tuple[int, int] | None:
    """The lines all ``changes`` add and remove, or None if any of them is None."""
    added = removed = 0
    for change in changes:
        if change is None:
            return None
        added, removed = added + change[0], removed + change[1]
    return added, removed


def _written(arguments: Mapping[str, Any], output: str) -> str | None:
    content = arguments.get("content")
    return f"✓ +{len(text_lines(content))}" if isinstance(content, str) else None


def _ran(arguments: Mapping[str, Any], output: str) -> str | None:
    return "✓"


def _is_match(line: str) -> bool:
    """Whether ``line`` of a Grep or Glob output is a match, not a blank or a note about them."""
    notes = (SEARCH_TALLY, SEARCH_TRUNCATED, SEARCH_PAGINATION)
    return bool(line.strip()) and not any(note.match(line) for note in notes)


def _found(arguments: Mapping[str, Any], output: str) -> str | None:
    lines = [] if output.strip() in NO_MATCHES else text_lines(output)
    count = sum(1 for line in lines if _is_match(line))
    return f"✓ {counted(count, 'match', 'matches')}"


RESULT_SUMMARIES: Mapping[str, ResultSummarizer] = MappingProxyType(
    {
        "Read": lambda arguments, output: f"✓ {line_count(output)}",
        "Edit": _edited,
        "MultiEdit": _multi_edited,
        "Write": _written,
        "Bash": _ran,
        "Grep": _found,
        "Glob": _found,
    }
)
"""How to sum up what a call to each of Claude Code's built-in tools did, when it succeeds."""


def tool_result_summary(
    name: str, arguments: Mapping[str, Any], output: str, is_error: bool
) -> str:
    """What a finished call to the tool ``name`` did, as you'd glance for it: ticked with lines
    added and removed for an edit, matches for a search, lines for the rest; crossed with a
    command's exit code, or the output's lines."""
    if is_error:
        return _failure_summary(name, output)
    summarize = RESULT_SUMMARIES.get(name)
    summary = None if summarize is None else summarize(arguments, output)
    return f"✓ {line_count(output)}" if summary is None else summary


def _failure_summary(name: str, output: str) -> str:
    if name != "Bash":
        return f"✗ {line_count(output)}"
    exit_code = EXIT_CODE.search(output)
    return "✗" if exit_code is None else f"✗ exit {exit_code[1]}"


def tool_call_line(call: ToolCall, frame: str) -> tuple[str, EntryKind]:
    """``call`` as its entry's one line and the kind it's shown as: its summary behind the
    spinner's ``frame`` while it runs, then marked with how it went and what it did (see
    :func:`tool_result_summary`)."""
    match call.status:
        case "running":
            return f"{frame}{call.summary}", "tool"
        case "passed":
            return f"→ {call.summary} {_result(call, is_error=False)}", "tool"
        case "failed":
            return f"→ {call.summary} {_result(call, is_error=True)}", "error"
        case "no_result":
            return f"→ {call.summary} ⊘ no result", "tool"
        case _:
            assert_never(call.status)


def _result(call: ToolCall, *, is_error: bool) -> str:
    return tool_result_summary(call.name, call.arguments, call.output, is_error)


def orbit_request_line(call: ToolCall, agent: str | None, frame: str) -> tuple[str, EntryKind]:
    """``agent``'s request ``call`` to orbit as its entry's one line and the kind it's shown
    as: its summary behind the spinner's ``frame`` while it waits, then marked with how it went
    and the first line of orbit's reply, clipped."""
    head = f"{agent} → orbit {call.summary}"
    match call.status:
        case "running":
            return f"{frame}{head}", "request"
        case "passed":
            return f"{head} {_replied('✓', call.output)}", "request"
        case "failed":
            return f"{head} {_replied('✗', call.output)}", "error"
        case "no_result":
            return f"{head} ⊘ no reply", "tool"
        case _:
            assert_never(call.status)


def _replied(mark: str, reply: str) -> str:
    """``mark`` followed by the first line of ``reply``, clipped, if it has one."""
    first = clipped(reply.strip().partition("\n")[0].strip(), REPLY_LIMIT)
    return f"{mark} {first}" if first else mark
