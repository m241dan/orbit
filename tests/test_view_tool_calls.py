from typing import Any

import pytest

from orbit.tui.view import (
    tool_call_summary,
)


@pytest.mark.parametrize(
    ("name", "arguments", "summary"),
    [
        ("Read", {"file_path": "src/a.py"}, "Read src/a.py"),
        ("Write", {"file_path": "src/a.py", "content": "x"}, "Write src/a.py"),
        ("Edit", {"file_path": "src/a.py", "old_string": "x"}, "Edit src/a.py"),
        ("MultiEdit", {"file_path": "src/a.py", "edits": []}, "MultiEdit src/a.py"),
        ("NotebookEdit", {"notebook_path": "n.ipynb"}, "NotebookEdit n.ipynb"),
        ("Bash", {"command": "uv run pytest"}, "Bash uv run pytest"),
        ("Grep", {"pattern": "def x"}, "Grep def x"),
        ("Grep", {"pattern": "def x", "path": "src"}, "Grep def x in src"),
        ("Glob", {"pattern": "*.py"}, "Glob *.py"),
        ("Glob", {"pattern": "*.py", "path": "tests"}, "Glob *.py in tests"),
        ("WebSearch", {"query": "textual"}, "WebSearch textual"),
        ("WebFetch", {"url": "https://x.org"}, "WebFetch https://x.org"),
        ("Task", {"description": "Find it", "prompt": "…"}, "Task Find it"),
        ("Agent", {"description": "Find it"}, "Agent Find it"),
        ("TodoWrite", {"todos": [{}, {}, {}]}, "TodoWrite 3 todos"),
    ],
)
def test_tool_call_summary_names_a_built_in_tools_key_argument(
    name: str, arguments: dict[str, Any], summary: str
) -> None:
    assert tool_call_summary(name, arguments) == summary


@pytest.mark.parametrize("field", ["summary", "title", "note", "reason", "name"])
def test_tool_call_summary_shows_an_orbit_tools_first_text_field(field: str) -> None:
    arguments = {field: "Fix it", "zzz": "other"}
    assert tool_call_summary("mcp__orbit__complete_task", arguments) == "complete_task Fix it"


def test_tool_call_summary_prefers_an_orbit_tools_earlier_field() -> None:
    arguments = {"name": "later", "summary": "first"}
    assert tool_call_summary("mcp__orbit__x", arguments) == "x first"


def test_tool_call_summary_shows_an_orbit_tool_without_a_text_field_by_name() -> None:
    assert tool_call_summary("mcp__orbit__get_task", {}) == "get_task"
    assert tool_call_summary("mcp__orbit__run_check", {"step": "lint"}) == "run_check"


def test_tool_call_summary_shows_an_unknown_tools_arguments_as_json() -> None:
    summary = tool_call_summary("upper", {"text": "hé"})
    assert summary == 'upper {"text": "hé"}'


def test_tool_call_summary_falls_back_to_json_without_its_key_argument() -> None:
    assert tool_call_summary("Read", {}) == "Read {}"
    assert tool_call_summary("Bash", {"command": 3}) == 'Bash {"command": 3}'
    assert tool_call_summary("TodoWrite", {"todos": "x"}) == 'TodoWrite {"todos": "x"}'
    assert tool_call_summary("Grep", {"pattern": "x", "path": 1}) == "Grep x"


def test_tool_call_summary_shows_a_commands_first_line() -> None:
    arguments = {"command": "cd  src;\techo hi\nls -la\npwd"}
    assert tool_call_summary("Bash", arguments) == "Bash cd src; echo hi"


@pytest.mark.parametrize("command", ["\nls -la\npwd", "  \n  ls -la", "\n\n ls  -la"])
def test_tool_call_summary_shows_a_commands_first_line_that_is_not_blank(command: str) -> None:
    assert tool_call_summary("Bash", {"command": command}) == "Bash ls -la"


@pytest.mark.parametrize(
    ("name", "arguments", "summary"),
    [
        ("Read", {"file_path": ""}, 'Read {"file_path": ""}'),
        ("Read", {"file_path": " \n "}, 'Read {"file_path": " \\n "}'),
        ("Bash", {"command": "\n  \n"}, 'Bash {"command": "\\n  \\n"}'),
        ("Grep", {"pattern": " ", "path": "src"}, 'Grep {"pattern": " ", "path": "src"}'),
    ],
)
def test_tool_call_summary_falls_back_to_json_with_a_blank_key_argument(
    name: str, arguments: dict[str, Any], summary: str
) -> None:
    assert tool_call_summary(name, arguments) == summary


def test_tool_call_summary_leaves_out_a_blank_search_path() -> None:
    assert tool_call_summary("Grep", {"pattern": "def x", "path": "  "}) == "Grep def x"


def test_tool_call_summary_skips_an_orbit_tools_blank_field() -> None:
    arguments = {"summary": "   ", "title": "Fix"}
    assert tool_call_summary("mcp__orbit__complete_task", arguments) == "complete_task Fix"


def test_tool_call_summary_shows_an_orbit_tool_with_only_blank_fields_by_name() -> None:
    arguments = {"summary": "", "note": "\n"}
    assert tool_call_summary("mcp__orbit__complete_task", arguments) == "complete_task"


def test_tool_call_summary_collapses_whitespace_in_its_key_argument() -> None:
    arguments = {"summary": "Add  a\nthing"}
    assert tool_call_summary("mcp__orbit__complete_task", arguments) == "complete_task Add a thing"


def test_tool_call_summary_keeps_120_characters() -> None:
    path = "a" * (120 - len("Read "))
    assert tool_call_summary("Read", {"file_path": path}) == f"Read {path}"


def test_tool_call_summary_cuts_121_characters_with_an_ellipsis() -> None:
    path = "a" * (121 - len("Read "))
    summary = tool_call_summary("Read", {"file_path": path})
    assert len(summary) == 120
    assert summary == f"Read {path[:114]}…"
