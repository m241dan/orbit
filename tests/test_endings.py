"""Why tasks end: given up, in error or by a bug, worded for the board."""

from orbit.agent.domain.endings import (
    BUG,
    ENDED_AT_PROPOSAL,
    NO_PROPOSAL,
    STATUSES,
    bug,
    error,
    first_line,
    gave_up,
    run_ending,
    task_ending,
    unaccepted,
)
from orbit.agent.domain.verification import VerificationStep, failure_reason, failure_report


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


def test_a_proposal_never_made_or_accepted_says_so() -> None:
    assert unaccepted(None) == (NO_PROPOSAL, "")


def test_a_proposal_that_last_failed_gives_the_step_and_its_report() -> None:
    step = VerificationStep("Lint", ("ruff", "check"))
    assert unaccepted((step, "E501")) == (
        "Proposed check Lint failed",
        failure_report(step, "E501"),
    )


def test_each_phase_ends_its_task_as_its_status() -> None:
    assert STATUSES == {
        "assigned": "error",
        "working": "error",
        "verifying": "error",
        "done": "completed",
        "fallback": "unverified",
        "gave_up": "gave_up",
        "ended": "cancelled",
    }


def test_a_fallback_after_a_failure_ends_unverified_for_that_step() -> None:
    step = VerificationStep("Lint", ("ruff", "check"))
    proposed = VerificationStep("Types", ("mypy",))
    ending = task_ending("fallback", (step, "E501"), (proposed, "error"), "", None)
    assert ending == ("unverified", failure_reason(step, "E501"))


def test_a_fallback_without_a_failure_ends_unverified_for_its_proposal() -> None:
    step = VerificationStep("Lint", ("ruff", "check"))
    ending = task_ending("fallback", None, (step, "E501"), "", None)
    assert ending == ("unverified", unaccepted((step, "E501")))


def test_a_fallback_without_any_failure_ends_unverified_without_a_proposal() -> None:
    assert task_ending("fallback", None, None, "", None) == ("unverified", (NO_PROPOSAL, ""))


def test_other_phases_end_as_their_run_ended() -> None:
    step = VerificationStep("Lint", ("ruff", "check"))
    assert task_ending("gave_up", None, None, "Too vague.", None) == (
        "gave_up",
        run_ending("gave_up", "gave_up", "Too vague.", None),
    )
    assert task_ending("working", (step, "E501"), None, "", "boom") == (
        "error",
        run_ending("error", "working", "", "boom"),
    )
    assert task_ending("done", None, None, "", None) == ("completed", ("", ""))
    assert task_ending("ended", None, None, "", None) == ("cancelled", (ENDED_AT_PROPOSAL, ""))
