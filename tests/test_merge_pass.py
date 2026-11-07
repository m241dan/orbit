"""What a merge pass decides of its review's findings: the suggestions and dismissals it makes,
what's refused, and what becomes of the findings as it, and the tasks it filed, end."""

from dataclasses import replace
from typing import Any

import pytest

from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.merge_holds import BlockingTask
from orbit.agent.domain.merge_pass import (
    NO_FINDING_NAMED,
    NO_REASON,
    NOT_BLOCKING,
    Dismissal,
    ReviewSuggestion,
    build_blocking_task,
    count_outcomes,
    end_task_in,
    list_undecided,
    parse_dismissal,
    parse_review_suggestion,
    refuse_decision,
    refuse_filing,
    refuse_severity,
    undecided_refusal,
    with_dismissed,
    with_filed,
    with_task_ended,
)
from orbit.agent.domain.reviews import Finding, ReviewGroup, ReviewJob, with_task
from orbit.protocol import TaskSpec

JOB = ReviewJob(commits=("c1",), round=0, batch=None, reviewed=("1",))


def finding(number: int) -> Finding:
    """Finding ``F<number>``, undecided."""
    return Finding(f"F{number}", "bugs-reviewer", f"Issue {number}", "", "normal")


GROUP = ReviewGroup(
    JOB,
    lenses=("2", "3"),
    ended=("2", "3"),
    findings=(finding(1), finding(2), finding(3)),
    stage="merging",
    merges=("4",),
)
"""A review whose lenses reported F1 to F3, merged by task 4."""


def outcomes(group: ReviewGroup) -> list[tuple[str, str, str | None, str | None]]:
    return [(f.id, f.outcome, f.task, f.reason) for f in group.findings]


# Suggestions and dismissals as agents make them


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"title": "Fix it", "prompt": "Now."}, ReviewSuggestion("Fix it", "Now.")),
        (
            {"title": "Fix it", "findings": ["F1", " F3", "F1"], "severity": "blocking"},
            ReviewSuggestion("Fix it", "", ("F1", "F3"), "blocking"),
        ),
        ({"title": "Fix it", "findings": None, "severity": None}, ReviewSuggestion("Fix it", "")),
        ({"title": "  "}, "A task needs a title."),
        (
            {"title": "Fix it", "findings": "F1"},
            'A task\'s findings are a list of finding ids, such as ["F1", "F3"].',
        ),
        (
            {"title": "Fix it", "findings": [1]},
            'A task\'s findings are a list of finding ids, such as ["F1", "F3"].',
        ),
        (
            {"title": "Fix it", "severity": "urgent"},
            'A task\'s severity is "blocking" or "normal", not \'urgent\'.',
        ),
    ],
)
def test_a_review_suggestion_is_read_from_its_arguments_or_refused(
    arguments: dict[str, Any], expected: ReviewSuggestion | str
) -> None:
    assert parse_review_suggestion(arguments) == expected


def test_a_dismissal_takes_its_finding_and_reason() -> None:
    assert parse_dismissal({"finding": " F2 ", "reason": " Covered. "}) == Dismissal(
        "F2", "Covered."
    )


def test_a_dismissal_without_a_reason_is_refused() -> None:
    assert parse_dismissal({"finding": "F2", "reason": "  "}) == NO_REASON


# Deciding


def test_filing_names_the_first_finding_filed_and_the_rest_merged_into_its_task() -> None:
    assert outcomes(with_filed(GROUP, ("F3", "F1"), "7")) == [
        ("F1", "merged", "7", None),
        ("F2", "undecided", None, None),
        ("F3", "filed", "7", None),
    ]


def test_dismissing_records_the_reason() -> None:
    dismissed = with_dismissed(GROUP, Dismissal("F2", "Covered."))
    assert outcomes(dismissed)[1] == ("F2", "dismissed", None, "Covered.")


def test_filing_must_name_a_finding() -> None:
    assert refuse_filing(GROUP, ()) == NO_FINDING_NAMED


def test_filing_refuses_the_first_finding_it_cant_decide() -> None:
    decided = with_filed(GROUP, ("F1",), "7")
    assert refuse_filing(decided, ("F2", "F9", "F1")) == (
        "F9 isn't an undecided finding of this review."
    )
    assert refuse_filing(decided, ("F2", "F3")) is None


BLOCKING_GROUP = replace(
    GROUP,
    findings=(
        finding(1),
        replace(finding(2), role="security-reviewer", severity="blocking"),
        finding(3),
    ),
)
"""A review whose lenses reported F1 to F3, F2 blocking, from the security lens."""


def test_filing_as_blocking_needs_a_blocking_finding_among_those_named() -> None:
    assert NOT_BLOCKING == "File as blocking only when a blocking finding is among findings."
    assert refuse_filing(BLOCKING_GROUP, ("F1", "F3"), "blocking") == NOT_BLOCKING
    assert refuse_filing(BLOCKING_GROUP, ("F1", "F2"), "blocking") is None
    assert refuse_filing(BLOCKING_GROUP, ("F1",), "normal") is None


def test_filing_as_blocking_with_no_finding_named_is_refused() -> None:
    assert refuse_severity((), "blocking") == NOT_BLOCKING
    assert refuse_severity((), "normal") is None


def test_a_blocking_task_names_the_lens_of_its_blocking_finding_named_first() -> None:
    batch_group = replace(BLOCKING_GROUP, job=replace(JOB, commits=("c1", "c2"), batch="batch-3"))
    suggestion = ReviewSuggestion("Fix it", "", ("F2", "F1"), "blocking")
    assert build_blocking_task(batch_group, suggestion) == BlockingTask(
        "security-reviewer", "c2", "Fix it", "batch-3"
    )


def test_a_blocking_task_is_raised_by_the_lens_of_its_first_blocking_finding() -> None:
    group = replace(
        BLOCKING_GROUP,
        findings=(
            replace(finding(1), role="tests-reviewer"),
            replace(finding(2), severity="blocking"),
            replace(finding(3), role="security-reviewer", severity="blocking"),
        ),
    )
    suggestion = ReviewSuggestion("Fix it", "", ("F1", "F3", "F2"), "blocking")
    assert build_blocking_task(group, suggestion) == BlockingTask(
        "security-reviewer", "c1", "Fix it", None, ("bugs-reviewer",)
    )


@pytest.mark.parametrize(
    "suggestion",
    [
        ReviewSuggestion("Fix it", "", ("F2",), "normal"),
        ReviewSuggestion("Fix it", "", (), "blocking"),
        ReviewSuggestion("Fix it", "", ("F9",), "blocking"),
        ReviewSuggestion("Fix it", "", ("F1", "F3"), "blocking"),
    ],
)
def test_a_normal_task_or_one_naming_no_blocking_finding_known_is_no_blocking_task(
    suggestion: ReviewSuggestion,
) -> None:
    assert build_blocking_task(BLOCKING_GROUP, suggestion) is None


@pytest.mark.parametrize(
    ("group", "expected"),
    [
        (with_filed(GROUP, ("F1",), "7"), "F1 is already filed."),
        (with_filed(GROUP, ("F3", "F1"), "7"), "F1 is already merged."),
        (with_task_ended(with_filed(GROUP, ("F1",), "7"), ("7",), "fixed"), "F1 is already fixed."),
        (
            with_task_ended(with_filed(GROUP, ("F1",), "7"), ("7",), "given_up"),
            "F1 is already given up.",
        ),
    ],
)
def test_a_finding_decided_once_isnt_decided_again(group: ReviewGroup, expected: str) -> None:
    assert refuse_decision(group, "F1") == expected


def test_completion_waits_for_every_finding() -> None:
    assert list_undecided(GROUP) == ("F1", "F2", "F3")
    filed = with_filed(GROUP, ("F1", "F3"), "7")
    assert undecided_refusal(filed) == (
        "Decide every finding first: F2 has no outcome. File them with suggest_task or dismiss"
        " them with dismiss_finding."
    )
    assert undecided_refusal(with_dismissed(filed, Dismissal("F2", "Covered."))) is None


# Ending


def test_a_merge_pass_ending_leaves_its_undecided_findings_unanswered() -> None:
    ended = end_task_in(with_filed(GROUP, ("F1",), "7"), "4")
    assert [f.outcome for f in ended.findings] == ["filed", "unanswered", "unanswered"]
    assert count_outcomes(ended) == {"filed": 1, "merged": 0, "dismissed": 0, "unanswered": 2}


def test_findings_wait_for_the_last_of_several_merge_passes() -> None:
    two = ReviewGroup(JOB, findings=GROUP.findings, stage="merging", merges=("4", "5"))
    first = end_task_in(two, "4")
    assert list_undecided(first) == ("F1", "F2", "F3")
    assert list_undecided(end_task_in(first, "5")) == ()


def test_a_lens_ending_decides_nothing() -> None:
    reviewing = ReviewGroup(JOB, lenses=("2",), findings=GROUP.findings)
    assert end_task_in(reviewing, "2").findings == GROUP.findings


def test_a_task_ending_marks_only_the_findings_filed_in_it() -> None:
    filed = with_filed(with_filed(GROUP, ("F1", "F2"), "7"), ("F3",), "8")
    fixed = with_task_ended(filed, ("7", "9"), "fixed")
    assert [f.outcome for f in fixed.findings] == ["fixed", "fixed", "filed"]


def test_only_a_task_booked_after_the_fan_in_is_a_merge_pass() -> None:
    reviewing = ReviewGroup(JOB, lenses=("2",))
    assert with_task(reviewing, "4") == reviewing
    assert with_task(GROUP, "5").merges == ("4", "5")
    assert with_task(GROUP, "4").merges == ("4",)


def test_a_task_lists_itself_and_the_tasks_it_fixes_on_the_board() -> None:
    board = TaskBoard()
    first = board.add(TaskSpec(prompt="Fix it."))
    fix = board.add(TaskSpec(prompt="Fix it."), fixes=first)
    again = board.add(TaskSpec(prompt="Fix it."), fixes=fix)
    assert board.list_fixed(board.get(again)) == (again, fix, first)
    assert board.list_fixed(board.get(first)) == (first,)
