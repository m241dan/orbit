from rich.text import Text

from orbit.protocol import (
    CODER,
    REVIEW_ORIGIN,
    REVIEWER,
    AgentEntry,
    TaskSpec,
    TaskState,
    TaskStatus,
    Usage,
)
from orbit.protocol import edit_refusal as shared_edit_refusal
from orbit.tui.view.rules import usage_text
from orbit.tui.view.tasks import (
    NEW_TASK_LABEL,
    TASK_EDITOR_HINT,
    TASK_PROMPT_TITLE,
    TASK_TITLE_PLACEHOLDER,
    TASK_TITLE_TITLE,
    acceptable,
    active_task,
    edit_refusal,
    follow_top,
    in_lane,
    join_prompt,
    lane_tag,
    lanes_present,
    new_task_row,
    next_lane,
    role_lanes,
    session_usage_subtitle,
    split_prompt,
    task_editor_title,
    task_list_title,
    task_numbers,
    ticking,
    usage_label,
    usage_unknown_roles,
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


def test_a_task_list_narrowed_to_a_lane_names_it_in_its_title() -> None:
    assert task_list_title(held=False, lane="review") == "Task list · review"
    assert task_list_title(held=True, lane="review") == "Task list · held · review"


def test_a_lane_other_than_work_is_a_dim_tag() -> None:
    tag = lane_tag("review")
    assert str(tag) == "  review"
    assert tag.style == "dim"


def test_the_work_lane_and_an_unknown_one_have_no_tag() -> None:
    assert str(lane_tag("work")) == ""
    assert str(lane_tag(None)) == ""


def test_each_roles_lane_comes_from_the_catalogs_agents() -> None:
    agents = (
        AgentEntry(name=CODER, label="Coder", conversational=False),
        AgentEntry(name=REVIEWER, label="Reviewer", conversational=False, lane="review"),
    )
    assert role_lanes(agents) == {CODER: "work", REVIEWER: "review"}


def test_the_roles_whose_usage_is_unknown_come_from_the_catalogs_agents() -> None:
    agents = (
        AgentEntry(name=CODER, label="Coder", conversational=False),
        AgentEntry(name=REVIEWER, label="Reviewer", conversational=False, usage_known=False),
    )
    assert usage_unknown_roles(agents) == frozenset({REVIEWER})


def roled(*roles: str) -> list[TaskState]:
    return [
        TaskState(id=str(i), task=TaskSpec(prompt=str(i), role=role))
        for i, role in enumerate(roles)
    ]


LANES = {"planner": "planner", CODER: "work", REVIEWER: "review", "security": "security"}


def test_the_lanes_present_are_those_of_the_tasks_in_the_catalogs_order() -> None:
    tasks = roled("security", REVIEWER, "security")
    assert lanes_present(tasks, LANES) == ("review", "security")
    assert lanes_present([*tasks, *roled(CODER)], LANES) == ("work", "review", "security")


def test_a_role_the_catalog_does_not_list_has_no_lane_present() -> None:
    assert lanes_present(roled("gone"), LANES) == ()


def test_a_lane_keeps_just_its_tasks_and_no_lane_keeps_them_all() -> None:
    tasks = roled(CODER, REVIEWER, "security", REVIEWER, "gone")
    assert [t.id for t in in_lane(tasks, LANES, "review")] == ["1", "3"]
    assert in_lane(tasks, LANES, None) == tuple(tasks)


def test_the_lane_filter_cycles_from_all_through_each_lane_back_to_all() -> None:
    present = ("review", "security")
    assert next_lane(None, present) == "review"
    assert next_lane("review", present) == "security"
    assert next_lane("security", present) is None


def test_the_lane_filter_stays_on_all_with_no_lanes_present() -> None:
    assert next_lane(None, ()) is None


def test_a_lane_filter_whose_lane_has_gone_goes_back_to_all() -> None:
    assert next_lane("security", ("review",)) is None


def test_the_task_editor_is_titled_for_a_new_task_or_an_edit() -> None:
    assert task_editor_title(new=True) == "New task"
    assert task_editor_title(new=False) == "Edit task"


def test_the_task_editor_hint_names_save_and_cancel() -> None:
    assert "ctrl+s save" in TASK_EDITOR_HINT
    assert TASK_EDITOR_HINT.endswith("esc cancel")


def test_a_three_line_row_above_the_active_row_shifts_where_it_follows_to() -> None:
    rows = [(0, 2), (2, 3), (5, 2), (7, 2)]
    # Below the view: its bottom comes up to the view's bottom, past the taller row.
    assert follow_top(rows, 3, 0, 6) == 3
    # Above the view: it comes in with the whole three-line row above it.
    assert follow_top(rows, 2, 7, 6) == 2
    assert follow_top(rows, 2, 7, 4) == 5


def test_edit_refusal_says_a_running_or_verifying_task_cant_be_edited() -> None:
    for status in ("running", "verifying"):
        state = TaskState(id="3", task=TaskSpec(prompt="go"), status=status)
        assert edit_refusal(state) == "Task '3' can't be edited while it runs."


def test_edit_refusal_says_a_review_orbit_queued_cant_be_edited() -> None:
    for role in ("reviewer", "super-reviewer"):
        task = TaskSpec(prompt="check", role=role)
        state = TaskState(id="1", task=task, origin=REVIEW_ORIGIN)
        assert edit_refusal(state) == "Reviews can't be edited; remove it instead."


def test_edit_refusal_allows_a_pending_coder_task_and_a_draft() -> None:
    for status in ("pending", "draft"):
        state = TaskState(id="1", task=TaskSpec(prompt="go"), status=status)
        assert edit_refusal(state) is None


def test_edit_refusal_allows_a_review_the_user_added_and_a_task_a_review_suggested() -> None:
    review = TaskState(id="1", task=TaskSpec(prompt="check", role="reviewer"))
    suggestion = TaskState(id="2", task=TaskSpec(prompt="tidy"), origin=REVIEW_ORIGIN)
    assert edit_refusal(review) is None
    assert edit_refusal(suggestion) is None


def test_edit_refusal_answers_as_the_backend_does() -> None:
    states = (
        TaskState(id="3", task=TaskSpec(prompt="go"), status="verifying"),
        TaskState(id="1", task=TaskSpec(prompt="check", role="super-reviewer"), origin="review"),
        TaskState(id="1", task=TaskSpec(prompt="check", role="reviewer")),
        TaskState(id="2", task=TaskSpec(prompt="tidy"), origin="review"),
    )
    for state in states:
        shared = shared_edit_refusal(state)
        assert edit_refusal(state) == (None if shared is None else shared.message)


def test_new_task_row_is_labelled() -> None:
    assert NEW_TASK_LABEL == "+ new task"


def test_session_usage_subtitle_is_empty_for_nothing_spent() -> None:
    assert session_usage_subtitle(Usage()) == ""


def test_session_usage_subtitle_shows_what_was_spent() -> None:
    usage = Usage(input_tokens=1_500, cost_usd=0.3)
    assert session_usage_subtitle(usage) == "session: 1.5k tok · $0.30"


def test_session_usage_subtitle_shows_a_cost_without_tokens() -> None:
    assert session_usage_subtitle(Usage(cost_usd=2.5)) == "session: 0 tok · $2.50"


def test_only_a_draft_can_be_accepted() -> None:
    draft, pending = states("draft", "pending")
    assert acceptable(draft)
    assert not acceptable(pending)


def test_the_spinners_tick_while_a_task_runs() -> None:
    assert ticking(states("completed", "running"))


def test_the_spinners_stop_with_no_task_running() -> None:
    assert not ticking(states("verifying", "pending"))


def test_the_new_task_row_is_dim_and_italic() -> None:
    assert new_task_row() == Text(NEW_TASK_LABEL, style="dim italic")


def test_the_task_editor_titles_its_text_fields_and_says_how_to_type() -> None:
    assert (TASK_TITLE_TITLE, TASK_PROMPT_TITLE) == ("Title", "Prompt")
    assert TASK_TITLE_PLACEHOLDER == "Press i to type"
