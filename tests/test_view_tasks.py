from orbit.protocol import (
    TaskSpec,
    TaskState,
    TaskStatus,
    Usage,
)
from orbit.tui.view import (
    active_task,
    follow_top,
    join_prompt,
    split_prompt,
    task_list_title,
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


def states(*statuses: TaskStatus) -> list[TaskState]:
    return [
        TaskState(id=str(i), task=TaskSpec(prompt=str(i)), status=status)
        for i, status in enumerate(statuses)
    ]


def test_no_task_running_or_verifying_means_no_active_task() -> None:
    assert active_task([]) is None
    assert active_task(states("completed", "pending", "draft", "error")) is None


def test_the_one_running_task_is_active() -> None:
    assert active_task(states("completed", "running", "pending")) == 1


def test_the_topmost_of_several_running_or_verifying_tasks_is_active() -> None:
    assert active_task(states("completed", "verifying", "running", "verifying")) == 1
    assert active_task(states("pending", "running", "verifying")) == 1


def test_a_verifying_task_is_active_whatever_its_role() -> None:
    review = TaskState(id="r", task=TaskSpec(prompt="r", role="reviewer"), status="verifying")
    assert active_task([*states("completed"), review]) == 1


TWO_LINE_ROWS = [(0, 2), (2, 2), (4, 2), (6, 2), (8, 2), (10, 2)]


def test_a_row_in_view_needs_no_scroll() -> None:
    assert follow_top(TWO_LINE_ROWS, 1, 0, 6) is None
    assert follow_top(TWO_LINE_ROWS, 4, 4, 6) is None


def test_a_row_below_the_view_scrolls_up_to_the_views_bottom() -> None:
    assert follow_top(TWO_LINE_ROWS, 4, 0, 6) == 4
    # Half in view counts as out of view.
    assert follow_top(TWO_LINE_ROWS, 2, 0, 5) == 1


def test_a_row_above_the_view_comes_in_with_the_row_above_it() -> None:
    assert follow_top(TWO_LINE_ROWS, 2, 8, 6) == 2
    assert follow_top(TWO_LINE_ROWS, 2, 5, 6) == 2


def test_the_top_row_needs_no_context_above_it() -> None:
    assert follow_top(TWO_LINE_ROWS, 0, 4, 6) == 0


def test_a_tall_row_shows_from_its_top_when_the_view_is_too_short() -> None:
    rows = [(0, 2), (2, 5), (7, 2)]
    # No room for the row above it.
    assert follow_top(rows, 1, 6, 6) == 2
    # Taller than the view: its top shows, whichever side it's on.
    assert follow_top(rows, 1, 6, 4) == 2
    assert follow_top(rows, 1, 0, 4) == 2


def test_a_held_task_list_says_so_in_its_title() -> None:
    assert task_list_title(held=False) == "Task list"
    assert task_list_title(held=True) == "Task list · held"
