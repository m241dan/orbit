"""Why tasks end: given up, in error or by a bug, worded for the board."""

from orbit.agent.domain.endings import (
    BUG,
    ENDED_AT_PROPOSAL,
    bug,
    error,
    first_line,
    gave_up,
    run_ending,
)


def test_the_first_line_skips_blank_lines_and_strips_it() -> None:
    assert first_line("\n  Too vague. \nMore.") == "Too vague."


def test_text_without_a_line_has_an_empty_first_line() -> None:
    assert first_line(" \n") == ""


def test_giving_up_is_headed_by_the_reasons_first_line_and_keeps_it_whole() -> None:
    reason = "The spec contradicts itself.\nIt asks for both."
    assert gave_up(reason) == ("Gave up: The spec contradicts itself.", reason)


def test_giving_up_without_a_reason_says_just_that() -> None:
    assert gave_up("") == ("Gave up", "")


def test_an_error_is_headed_by_the_messages_first_line_and_keeps_it_whole() -> None:
    message = "git worktree add failed\nno space left"
    assert error(message) == ("Error: git worktree add failed", message)


def test_a_bug_keeps_the_exceptions_text() -> None:
    assert bug(ValueError("boom")) == (BUG, "boom")


def test_a_run_that_errs_without_a_message_says_what_its_phase_means() -> None:
    assert run_ending("error", "assigned", "", None) == (
        "Error: The agent exited before fetching its task",
        "The agent exited before fetching its task",
    )


def test_a_run_that_errs_gives_its_message() -> None:
    assert run_ending("error", "working", "", "agent-1 ran out of time (5 minutes).") == (
        "Error: agent-1 ran out of time (5 minutes).",
        "agent-1 ran out of time (5 minutes).",
    )


def test_a_run_that_gave_up_gives_the_agents_reason() -> None:
    assert run_ending("gave_up", "gave_up", "Too vague.", None) == (
        "Gave up: Too vague.",
        "Too vague.",
    )


def test_a_run_that_ended_cancelled_was_ended_at_its_proposal() -> None:
    assert run_ending("cancelled", "ended", "", None) == (ENDED_AT_PROPOSAL, "")


def test_a_run_that_completed_has_no_reason() -> None:
    assert run_ending("completed", "done", "", None) == ("", "")
