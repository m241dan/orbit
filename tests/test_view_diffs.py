"""An editing tool's call is turned into the lines of the diff it makes."""

from typing import Any

import pytest

from orbit.tui.view.diffs import EDIT_SEPARATOR, FileDiff, diff_change, file_diff, line_diff
from orbit.tui.view.tool_calls import tool_result_summary


def test_unchanged_lines_are_kept_around_the_removed_then_added_ones() -> None:
    assert line_diff("a\nb\nc", "a\nB\nc\nd") == [" a", "-b", "+B", " c", "+d"]


def test_removing_everything_marks_each_line_removed() -> None:
    assert line_diff("a\nb", "") == ["-a", "-b"]


def test_an_edit_diffs_its_old_string_against_its_new_one() -> None:
    arguments = {"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 2"}
    assert file_diff("Edit", arguments) == FileDiff("a.py", ("-x = 1", "+x = 2"))


def test_replace_all_doesnt_change_an_edits_diff() -> None:
    arguments = {"file_path": "a.py", "old_string": "x", "new_string": "y"}
    assert file_diff("Edit", {**arguments, "replace_all": True}) == file_diff("Edit", arguments)


def test_a_multi_edit_separates_the_diffs_of_its_edits() -> None:
    edits = [{"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": "c"}]
    diff = file_diff("MultiEdit", {"file_path": "a.py", "edits": edits})
    assert diff == FileDiff("a.py", ("-a", "+b", EDIT_SEPARATOR, " c"))


def test_a_write_adds_every_line_of_its_content() -> None:
    diff = file_diff("Write", {"file_path": "a.py", "content": "one\n\ntwo\n"})
    assert diff == FileDiff("a.py", ("+one", "+", "+two"))


def test_a_write_adds_a_line_holding_a_form_feed_as_one_line() -> None:
    diff = file_diff("Write", {"file_path": "a.py", "content": "a\x0cb\nc\n"})
    assert diff == FileDiff("a.py", ("+a\x0cb", "+c"))


def test_an_edit_diffs_a_line_holding_a_form_feed_as_one_line() -> None:
    diff = file_diff("Edit", {"file_path": "a.py", "old_string": "a", "new_string": "b\x0cc"})
    assert diff == FileDiff("a.py", ("-a", "+b\x0cc"))


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("Read", {"file_path": "a.py"}),
        ("Edit", {"old_string": "a", "new_string": "b"}),
        ("Edit", {"file_path": "a.py", "old_string": "a"}),
        ("Edit", {"file_path": "a.py", "old_string": 1, "new_string": "b"}),
        ("MultiEdit", {"file_path": "a.py", "edits": "a"}),
        ("MultiEdit", {"file_path": "a.py", "edits": ["a"]}),
        ("MultiEdit", {"file_path": "a.py", "edits": [{"old_string": "a", "new_string": 2}]}),
        ("Write", {"file_path": "a.py"}),
    ],
)
def test_other_tools_and_malformed_arguments_have_no_diff(
    name: str, arguments: dict[str, Any]
) -> None:
    assert file_diff(name, arguments) is None


def test_an_edits_change_counts_its_added_and_removed_lines() -> None:
    arguments = {"old_string": "a\nb\nc", "new_string": "a\nB\nc\nd"}
    assert diff_change("Edit", arguments) == (2, 1)


def test_a_multi_edits_change_counts_every_edit_but_not_the_separator() -> None:
    edits = [{"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": "c\nd"}]
    assert diff_change("MultiEdit", {"edits": edits}) == (2, 1)


def test_a_multi_edit_with_an_invalid_edit_has_no_change() -> None:
    edits = [{"old_string": "a", "new_string": "b"}, {"old_string": "c"}]
    assert diff_change("MultiEdit", {"edits": edits}) is None


def test_a_writes_change_adds_every_line_of_its_content() -> None:
    assert diff_change("Write", {"content": "one\n\ntwo\n"}) == (3, 0)


def test_another_tool_has_no_change() -> None:
    assert diff_change("Read", {"file_path": "a.py"}) is None


def test_an_edits_summary_counts_the_lines_its_diff_shows() -> None:
    arguments = {"file_path": "a.py", "old_string": "a\nb\nc\ne", "new_string": "a\nB\nc\nd"}
    diff = file_diff("Edit", arguments)
    assert diff is not None
    added = sum(line.startswith("+") for line in diff.lines)
    removed = sum(line.startswith("-") for line in diff.lines)
    summary = tool_result_summary("Edit", arguments, "", is_error=False)
    assert summary == f"✓ +{added} \N{MINUS SIGN}{removed}"
