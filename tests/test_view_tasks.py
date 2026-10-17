from orbit.protocol import (
    TaskSpec,
    TaskState,
    Usage,
)
from orbit.tui.view import (
    join_prompt,
    split_prompt,
    task_numbers,
    usage_label,
    usage_text,
)


def test_planner_prompt_splits_into_title_and_body_and_joins_back() -> None:
    prompt = "Title\n\nBody line 1\nBody line 2"
    assert split_prompt(prompt) == ("Title", "Body line 1\nBody line 2")
    assert join_prompt(*split_prompt(prompt)) == prompt


def test_fix_prompt_round_trips_through_split_and_join() -> None:
    prompt = "Fix the failed verification step: pytest\n\n$ uv run pytest\n...\nMore"
    assert join_prompt(*split_prompt(prompt)) == prompt


def test_single_line_prompt_has_no_body() -> None:
    assert split_prompt("Do the thing") == ("Do the thing", "")
    assert join_prompt(*split_prompt("Do the thing")) == "Do the thing"


def test_empty_prompt_splits_to_nothing() -> None:
    assert split_prompt("") == ("", "")


def test_body_on_the_second_line_joins_back_after_a_blank_line() -> None:
    assert split_prompt("a\nb") == ("a", "b")
    assert join_prompt("a", "b") == "a\n\nb"


def test_blank_lines_and_trailing_whitespace_around_the_body_are_dropped() -> None:
    assert split_prompt("Title\n  \n\n  body\n\n  ") == ("Title", "  body")
    assert join_prompt("Title", "\n  body  \n\n") == "Title\n\n  body"


def test_body_keeps_its_leading_indentation_through_split_and_join() -> None:
    prompt = "Title\n\n    code = 1\nmore\n"
    assert split_prompt(prompt) == ("Title", "    code = 1\nmore")
    assert join_prompt(*split_prompt(prompt)) == "Title\n\n    code = 1\nmore"


def test_joining_a_blank_body_gives_just_the_title() -> None:
    assert join_prompt("Title", "") == "Title"
    assert join_prompt("Title", "  \n ") == "Title"


def test_joining_strips_the_title() -> None:
    assert join_prompt("  Title \n", "body") == "Title\n\nbody"


def test_interior_blank_lines_of_the_body_survive_split_and_join() -> None:
    prompt = "Title\n\na\n\n  \nb"
    assert split_prompt(prompt) == ("Title", "a\n\n  \nb")
    assert join_prompt(*split_prompt(prompt)) == prompt


def test_trailing_whitespace_on_an_interior_body_line_survives() -> None:
    assert split_prompt("Title\n\na  \nb") == ("Title", "a  \nb")


def test_only_newlines_delimit_body_lines() -> None:
    # str.splitlines would also split on these; the editor must hand the body back unchanged.
    for prompt in ("Title\n\na\u2028b", "Title\n\na\x0cb", "Title\n\na\r\nb"):
        assert join_prompt(*split_prompt(prompt)) == prompt


def test_task_numbers_number_a_fix_whose_task_is_missing_as_a_top_level_task() -> None:
    orphan = TaskState(id="2", task=TaskSpec(prompt="fix"), fixes="gone")
    fix = TaskState(id="3", task=TaskSpec(prompt="fix"), fixes="2")
    assert task_numbers([orphan, fix]) == ["1", "1.1"]


def test_task_list_and_turn_rule_read_the_same_count_alike() -> None:
    usage = Usage(input_tokens=14_500)
    assert usage_label(usage) == "14k tok · $0.00"
    assert usage_text(usage) == "↑14k ↓0"
