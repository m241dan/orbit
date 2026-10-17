"""An editing tool's call is turned into the lines of the diff it makes."""

from typing import Any

import pytest

from orbit.tui.view import EDIT_SEPARATOR, FileDiff, file_diff, line_diff


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
