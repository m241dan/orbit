from typing import Any

import pytest

from orbit.protocol import ToolCallFinished, ToolCallStarted
from orbit.tui.view import (
    ToolCall,
    line_count,
    tool_call_finished,
    tool_call_line,
    tool_call_started,
    tool_call_summary,
    tool_call_title,
    tool_call_unfinished,
    tool_result_summary,
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


def test_a_tool_calls_title_is_its_summary() -> None:
    assert tool_call_title("Read", {"file_path": "a.py"}) == "Read a.py"


def test_a_tool_call_without_arguments_is_titled_by_the_tool_alone() -> None:
    assert tool_call_title("Read", {}) == "Read"


def started_call(call_id: str = "c") -> ToolCall:
    arguments = {"file_path": "a.py"}
    event = ToolCallStarted(turn_id="t", call_id=call_id, name="Read", arguments=arguments)
    return tool_call_started(event)


def test_a_started_call_runs_keeping_its_id_and_full_arguments() -> None:
    assert started_call() == ToolCall(
        "c", "Read", "Read a.py", {"file_path": "a.py"}, "running", ""
    )


def test_a_finished_call_keeps_its_output() -> None:
    event = ToolCallFinished(turn_id="t", call_id="c", output="one\ntwo")
    assert tool_call_finished(started_call(), event).output == "one\ntwo"


@pytest.mark.parametrize(("is_error", "status"), [(False, "passed"), (True, "failed")])
def test_a_finished_call_passed_or_failed_by_its_error(is_error: bool, status: str) -> None:
    event = ToolCallFinished(turn_id="t", call_id="c", output="", is_error=is_error)
    assert tool_call_finished(started_call(), event).status == status


def test_a_running_call_ends_with_no_result() -> None:
    assert tool_call_unfinished(started_call()).status == "no_result"


def test_a_finished_call_keeps_its_result_when_its_turn_ends() -> None:
    event = ToolCallFinished(turn_id="t", call_id="c", output="ok")
    finished = tool_call_finished(started_call(), event)
    assert tool_call_unfinished(finished) == finished


@pytest.mark.parametrize(
    ("text", "count"),
    [("", "0 lines"), ("one", "1 line"), ("one\n", "1 line"), ("one\ntwo", "2 lines")],
)
def test_line_counts_read_in_the_singular_only_for_one(text: str, count: str) -> None:
    assert line_count(text) == count


def test_a_running_calls_line_puts_the_spinner_before_its_summary() -> None:
    assert tool_call_line(started_call(), "◠ ") == ("◠ Read a.py", "tool")


def test_a_passed_calls_line_is_ticked_with_its_line_count() -> None:
    call = ToolCall("c", "Read", "Read a.py", {}, "passed", "a\nb")
    assert tool_call_line(call, "◠ ") == ("→ Read a.py ✓ 2 lines", "tool")


def test_a_failed_calls_line_is_crossed_as_an_error() -> None:
    call = ToolCall("c", "Read", "Read a.py", {}, "failed", "")
    assert tool_call_line(call, "◠ ") == ("→ Read a.py ✗ 0 lines", "error")


def test_a_call_with_no_result_says_so_muted() -> None:
    call = ToolCall("c", "Read", "Read a.py", {}, "no_result")
    assert tool_call_line(call, "◠ ") == ("→ Read a.py ⊘ no result", "tool")


def test_a_calls_line_sums_up_its_result_by_its_tool() -> None:
    call = ToolCall("c", "Bash", "Bash ls", {"command": "ls"}, "passed", "a\nb")
    assert tool_call_line(call, "◠ ") == ("→ Bash ls ✓", "tool")


def test_a_failed_calls_line_sums_up_its_result_by_its_tool() -> None:
    call = ToolCall("c", "Bash", "Bash ls", {"command": "ls"}, "failed", "Exit code 2")
    assert tool_call_line(call, "◠ ") == ("→ Bash ls ✗ exit 2", "error")


def test_a_reads_result_counts_its_lines() -> None:
    assert tool_result_summary("Read", {"file_path": "a"}, "a\nb\nc", False) == "✓ 3 lines"


@pytest.mark.parametrize(
    ("old", "new", "summary"),
    [
        ("a", "b", "✓ +1 \N{MINUS SIGN}1"),
        ("a\nb\nc", "a\nB\nc", "✓ +1 \N{MINUS SIGN}1"),
        ("a\nc", "a\nb1\nb2\nc", "✓ +2 \N{MINUS SIGN}0"),
        ("a\nb\nc", "a", "✓ +0 \N{MINUS SIGN}2"),
        ("", "x\ny", "✓ +2 \N{MINUS SIGN}0"),
    ],
)
def test_an_edits_result_counts_the_lines_it_changes_leaving_out_unchanged_ones(
    old: str, new: str, summary: str
) -> None:
    arguments = {"file_path": "a", "old_string": old, "new_string": new}
    assert tool_result_summary("Edit", arguments, "ok", False) == summary


def test_a_multi_edits_result_sums_its_edits_changes() -> None:
    edits = [
        {"old_string": "a", "new_string": "b\nc"},
        {"old_string": "x\ny\nz", "new_string": "x"},
    ]
    assert tool_result_summary("MultiEdit", {"edits": edits}, "ok", False) == "✓ +2 \N{MINUS SIGN}3"


def test_a_multi_edit_without_edits_changes_nothing() -> None:
    assert tool_result_summary("MultiEdit", {"edits": []}, "ok", False) == "✓ +0 \N{MINUS SIGN}0"


def test_a_writes_result_counts_the_lines_it_wrote() -> None:
    arguments = {"file_path": "a", "content": "a\nb\n"}
    assert tool_result_summary("Write", arguments, "ok", False) == "✓ +2"


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("Edit", {}),
        ("Edit", {"old_string": "a"}),
        ("Edit", {"old_string": 1, "new_string": "b"}),
        ("MultiEdit", {}),
        ("MultiEdit", {"edits": "a"}),
        ("MultiEdit", {"edits": [{"old_string": "a", "new_string": "b"}, "c"]}),
        ("MultiEdit", {"edits": [{"old_string": "a"}]}),
        ("Write", {}),
        ("Write", {"content": ["a"]}),
    ],
)
def test_a_result_with_malformed_arguments_counts_its_outputs_lines(
    name: str, arguments: dict[str, Any]
) -> None:
    assert tool_result_summary(name, arguments, "a\nb", False) == "✓ 2 lines"


def test_a_commands_result_is_ticked_alone() -> None:
    assert tool_result_summary("Bash", {"command": "ls"}, "a\nb", False) == "✓"


@pytest.mark.parametrize(
    ("output", "summary"),
    [
        ("Exit code 1", "✗ exit 1"),
        ("boom\nExit code 127\nmore\nExit code 3", "✗ exit 127"),
        ("Exit code 12abc", "✗"),
        ("not Exit code 2", "✗"),
        ("boom", "✗"),
        ("", "✗"),
    ],
)
def test_a_failed_commands_result_shows_its_exit_code_when_it_has_one(
    output: str, summary: str
) -> None:
    assert tool_result_summary("Bash", {"command": "ls"}, output, True) == summary


@pytest.mark.parametrize("name", ["Grep", "Glob"])
@pytest.mark.parametrize(
    ("output", "summary"),
    [
        ("a.py\nb.py\n\nc.py\n", "✓ 3 matches"),
        ("a.py", "✓ 1 match"),
        ("", "✓ 0 matches"),
        ("No matches found", "✓ 0 matches"),
        ("No files found", "✓ 0 matches"),
        ("Found 2 files\na.py\nb.py", "✓ 2 matches"),
        ("a.py:3\nb.py:1\n\nFound 4 total occurrences across 2 files.", "✓ 2 matches"),
    ],
)
def test_a_searchs_result_counts_its_matches(name: str, output: str, summary: str) -> None:
    assert tool_result_summary(name, {"pattern": "x"}, output, False) == summary


@pytest.mark.parametrize("name", ["Read", "Edit", "Write", "Grep", "mcp__x__y", "WebFetch"])
def test_any_other_failed_result_counts_its_outputs_lines(name: str) -> None:
    assert tool_result_summary(name, {}, "boom\nExit code 1", True) == "✗ 2 lines"


@pytest.mark.parametrize("name", ["WebFetch", "mcp__orbit__get_task", "Agent"])
def test_any_other_tools_result_counts_its_outputs_lines(name: str) -> None:
    assert tool_result_summary(name, {}, "a", False) == "✓ 1 line"
