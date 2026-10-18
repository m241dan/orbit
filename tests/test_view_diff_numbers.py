"""An edit's diff is numbered by where its new lines are found in the edited file."""

from orbit.tui.view.diff_numbers import diff_numbers, edited_snippet, numbered_diff
from orbit.tui.view.diffs import EDIT_SEPARATOR, line_diff

HEADER = (
    "The file /src/a.py has been updated. Here's the result of running `cat -n` on a snippet "
    "of the edited file:"
)
ARROW_RESULT = f"{HEADER}\n   153→def go():\n   154→    x = 2\n   155→    return x\n   156→"
TAB_RESULT = f"{HEADER}\n   153\tdef go():\n   154\t    x = 2\n   155\t    return x\n   156\t"


def test_a_snippet_is_read_with_arrows() -> None:
    assert edited_snippet(ARROW_RESULT) == [
        (153, "def go():"),
        (154, "    x = 2"),
        (155, "    return x"),
        (156, ""),
    ]


def test_a_snippet_is_read_with_tabs() -> None:
    assert edited_snippet(TAB_RESULT) == edited_snippet(ARROW_RESULT)


def test_a_snippet_line_holding_a_form_feed_stays_one_line() -> None:
    assert edited_snippet(f"{HEADER}\n     7→a\x0cb\n     8→c") == [(7, "a\x0cb"), (8, "c")]


def test_an_edit_is_numbered_from_where_its_new_lines_are_found() -> None:
    lines = line_diff("    x = 1\n    return x", "    x = 2\n    return x")
    assert diff_numbers("Edit", lines, TAB_RESULT) == [None, 154, 155]


def test_an_edit_whose_new_lines_arent_found_has_no_numbers() -> None:
    lines = line_diff("x = 1", "x = 3")
    assert diff_numbers("Edit", lines, ARROW_RESULT) == [None, None]


def test_an_edit_whose_new_lines_run_past_the_snippet_has_no_numbers() -> None:
    lines = line_diff("", "    return x\n\nmore")
    assert diff_numbers("Edit", lines, ARROW_RESULT) == [None, None, None]


def test_an_edit_whose_new_lines_are_found_twice_has_no_numbers() -> None:
    result = f"{HEADER}\n     1→x\n     2→y\n     3→x\n     4→y"
    assert diff_numbers("Edit", line_diff("x", "x\ny"), result) == [None, None]


def test_the_following_lines_decide_between_two_places_for_the_first() -> None:
    result = f"{HEADER}\n     1→x\n     2→y\n     3→x\n     4→z"
    assert diff_numbers("Edit", line_diff("x", "x\nz"), result) == [3, 4]


def test_an_edit_starting_and_ending_mid_line_is_numbered_where_it_fits() -> None:
    result = f"{HEADER}\n     4→def f(a, b):\n     5→    y = foo(a,\n     6→        b)\n     7→"
    lines = line_diff("bar(a,\n        b", "foo(a,\n        b")
    assert diff_numbers("Edit", lines, result) == [None, 5, 6]


def test_an_edit_also_fitting_mid_line_elsewhere_has_no_numbers() -> None:
    result = f"{HEADER}\n     1→x\n     2→y\n     3→ax\n     4→y"
    assert diff_numbers("Edit", line_diff("q\ny", "x\ny"), result) == [None, None, None]


def test_an_edits_middle_lines_must_equal_whole_lines() -> None:
    result = f"{HEADER}\n     1→a\n     2→xbx\n     3→c\n     4→za\n     5→b\n     6→c"
    assert diff_numbers("Edit", line_diff("q", "a\nb\nc"), result) == [None, 4, 5, 6]


def test_an_edits_first_line_must_end_its_line() -> None:
    result = f"{HEADER}\n     1→xay\n     2→b\n     3→a\n     4→b"
    assert diff_numbers("Edit", line_diff("q", "a\nb"), result) == [None, 3, 4]


def test_an_edits_last_line_must_start_its_line() -> None:
    result = f"{HEADER}\n     1→a\n     2→xby\n     3→a\n     4→b"
    assert diff_numbers("Edit", line_diff("q", "a\nb"), result) == [None, 3, 4]


def test_a_one_line_edit_also_fitting_inside_another_line_has_no_numbers() -> None:
    result = f"{HEADER}\n     1→def f(x):\n     2→    f(x)\n     3→)"
    assert diff_numbers("Edit", line_diff("x)", ")"), result) == [None, None]


def test_a_one_line_edit_inside_a_line_is_numbered() -> None:
    assert diff_numbers("Edit", line_diff("1", "= 2"), ARROW_RESULT) == [None, 154]


def test_an_empty_new_line_fits_every_line() -> None:
    assert diff_numbers("Edit", line_diff("x", "\n"), ARROW_RESULT) == [None, None]


def test_a_found_edit_needs_its_lines_numbered_one_after_another() -> None:
    result = f"{HEADER}\n     1→x\n     5→z"
    assert diff_numbers("Edit", line_diff("x", "x\nz"), result) == [None, None]


def test_an_edit_that_removes_everything_has_no_numbers() -> None:
    assert diff_numbers("Edit", line_diff("x", ""), ARROW_RESULT) == [None]


def test_output_without_a_snippet_has_no_numbers() -> None:
    assert diff_numbers("Edit", line_diff("x", "def go():"), "Updated.") == [None, None]


def test_a_multi_edit_numbers_each_edit_it_finds() -> None:
    lines = [*line_diff("x", "def go():"), EDIT_SEPARATOR, *line_diff("y", "missing")]
    assert diff_numbers("MultiEdit", lines, ARROW_RESULT) == [None, 153, None, None, None]


def test_a_write_is_numbered_from_one() -> None:
    assert diff_numbers("Write", ["+a", "+b"], "Created.") == [1, 2]


def test_numbers_are_right_aligned_in_a_column_before_the_lines() -> None:
    assert numbered_diff(["-a", "+b", EDIT_SEPARATOR, " c"], [None, 9, None, 10]) == (
        ["   -a", " 9 +b", "   ⋯", "10  c"],
        3,
    )


def test_a_diff_without_numbers_has_no_column() -> None:
    assert numbered_diff(["-a", "+b"], [None, None]) == (["-a", "+b"], 0)
