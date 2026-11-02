"""The rules of findings and reviews that keep a group: a finding as an agent reports it, why
one is refused, how findings are numbered, when ``reviews_finished`` is due and a review is over,
and the fields and variables ``reviews_finished`` gives."""

from typing import Any

import pytest

from orbit.agent.domain.reviews import (
    BLOCKING_REFUSED,
    MESSAGES_LIMIT,
    Finding,
    FindingReport,
    MergeBriefing,
    ReviewGroup,
    ReviewJob,
    fan_in_due,
    finding_refusal,
    finding_report,
    finished_fields,
    finished_values,
    quotes_messages,
    review_over,
    set_review,
    with_ended,
    with_finding,
    with_lens,
)
from orbit.agent.domain.roles import Role

COMMIT = ReviewJob(commits=("1a2b3c4d",), round=0, batch=None, reviewed=("1",))
BATCH = ReviewJob(commits=("c1", "c2", "9f8e7d6c"), round=1, batch="batch-2")
SUPER = ReviewJob(commits=("5e6f7a8b9",), round=0, batch=None, whole=True)
SET = set_review("mutation", "1a2b3c4d")

LENS = Role(instructions="Look.", verify=False)
BLOCKER = LENS.model_copy(update={"blocking_findings": True})

NORMAL = FindingReport("Off by one", "parse() stops early.", "normal")
BLOCKING = FindingReport("Crash on empty input", "parse('') raises.", "blocking")


def reported(group: ReviewGroup, reporter: str, *reports: FindingReport) -> ReviewGroup:
    """``group`` with each of ``reports`` from a task of ``reporter`` among its findings."""
    for report in reports:
        group, _ = with_finding(group, reporter, report)
    return group


# A finding as reported


def test_a_reported_finding_has_its_title_on_one_line_and_its_body_stripped() -> None:
    arguments = {
        "title": "  Off\n by   one ",
        "body": "\nparse() stops early.\n",
        "severity": "normal",
    }
    assert finding_report(arguments) == FindingReport(
        "Off by one", "parse() stops early.", "normal"
    )


def test_a_reported_finding_without_a_body_has_an_empty_one() -> None:
    report = finding_report({"title": "Off by one", "severity": "blocking"})
    assert report == FindingReport("Off by one", "", "blocking")


@pytest.mark.parametrize(
    ("arguments", "refusal"),
    [
        ({"title": " ", "body": "x", "severity": "normal"}, "A finding needs a title."),
        (
            {"title": "Off by one", "severity": "urgent"},
            'A finding\'s severity is "blocking" or "normal", not \'urgent\'.',
        ),
        (
            {"title": "Off by one"},
            'A finding\'s severity is "blocking" or "normal", not None.',
        ),
    ],
)
def test_a_reported_finding_is_refused_without_a_title_or_a_known_severity(
    arguments: dict[str, Any], refusal: str
) -> None:
    assert finding_report(arguments) == refusal


# Refusals


def test_a_blocking_finding_from_a_role_without_blocking_findings_is_refused() -> None:
    assert finding_refusal(ReviewGroup(COMMIT), "tests-reviewer", LENS, BLOCKING, 10) == (
        BLOCKING_REFUSED
    )
    assert BLOCKING_REFUSED == (
        "Only a role with blocking_findings = true may report a blocking finding; report it as"
        " normal."
    )


def test_a_blocking_finding_from_a_role_with_blocking_findings_is_taken() -> None:
    assert finding_refusal(ReviewGroup(COMMIT), "bugs-reviewer", BLOCKER, BLOCKING, 10) is None


def test_a_normal_finding_from_any_role_is_taken() -> None:
    assert finding_refusal(ReviewGroup(COMMIT), "tests-reviewer", LENS, NORMAL, 10) is None


def test_a_finding_past_the_limit_from_one_lens_is_refused() -> None:
    group = reported(ReviewGroup(COMMIT), "bugs-reviewer", *[NORMAL] * 10)
    assert finding_refusal(group, "bugs-reviewer", LENS, NORMAL, 10) == (
        "This review has already reported 10 findings, the most allowed."
    )


def test_another_lenses_findings_dont_count_toward_a_lenses_limit() -> None:
    group = reported(ReviewGroup(COMMIT), "bugs-reviewer", *[NORMAL] * 2)
    assert finding_refusal(group, "tests-reviewer", LENS, NORMAL, 2) is None


# Numbering


def test_findings_are_numbered_within_their_review_in_the_order_reported() -> None:
    group, first = with_finding(ReviewGroup(COMMIT), "bugs-reviewer", BLOCKING)
    group, second = with_finding(group, "tests-reviewer", NORMAL)
    assert first == Finding(
        "F1", "bugs-reviewer", "Crash on empty input", "parse('') raises.", "blocking"
    )
    assert (second.id, second.role, second.outcome, second.task, second.reason) == (
        "F2",
        "tests-reviewer",
        "undecided",
        None,
        None,
    )
    assert group.findings == (first, second)


# Fan-in and the end of a review


def test_a_review_whose_lenses_have_all_ended_is_due_to_fan_in() -> None:
    group = with_ended(with_ended(with_lens(with_lens(ReviewGroup(COMMIT), "2"), "3"), "3"), "2")
    assert fan_in_due(group)


def test_a_review_with_a_lens_still_reviewing_isnt_due_to_fan_in() -> None:
    group = with_ended(with_lens(with_lens(ReviewGroup(COMMIT), "2"), "3"), "3")
    assert not fan_in_due(group)


def test_a_review_without_lenses_never_fans_in() -> None:
    assert not fan_in_due(with_ended(ReviewGroup(COMMIT), "2"))


def test_a_review_fans_in_once() -> None:
    group = ReviewGroup(COMMIT, lenses=("2",), ended=("2",), stage="merging")
    assert not fan_in_due(group)


def test_a_task_ended_or_booked_twice_is_kept_once_in_order() -> None:
    group = with_lens(with_lens(with_lens(ReviewGroup(COMMIT), "3"), "2"), "3")
    assert with_ended(with_ended(group, "2"), "2").ended == ("2",)
    assert group.lenses == ("3", "2")


def test_a_review_without_lenses_is_over_once_its_tasks_have_ended() -> None:
    group = with_ended(ReviewGroup(COMMIT), "2")
    assert not review_over(group, ("2", "3"))
    assert review_over(with_ended(group, "3"), ("2", "3"))


def test_a_review_with_lenses_is_over_only_once_it_fanned_in_and_its_tasks_ended() -> None:
    ended = ReviewGroup(COMMIT, lenses=("2",), ended=("2",))
    assert not review_over(ended, ("2",))
    merging = ReviewGroup(COMMIT, lenses=("2",), ended=("2",), stage="merging")
    assert review_over(merging, ("2",))
    assert not review_over(merging, ("2", "4"))


# reviews_finished


@pytest.mark.parametrize(
    ("job", "review"), [(COMMIT, "commit"), (BATCH, "batch"), (SUPER, "super"), (SET, "set")]
)
def test_reviews_finished_says_what_the_review_reviewed(job: ReviewJob, review: str) -> None:
    assert finished_fields(job, ())["review"] == review


def test_a_sets_report_is_reviewed_at_round_0_of_its_one_commit_reviewing_no_task() -> None:
    assert ReviewJob(commits=("1a2b3c4d",), round=0, batch=None, check_set="mutation") == SET


def test_reviews_finished_says_whether_there_were_findings() -> None:
    finding = Finding("F1", "bugs-reviewer", "Off by one", "", "normal")
    assert finished_fields(COMMIT, ())["findings"] == "none"
    assert finished_fields(COMMIT, (finding,))["findings"] == "some"


@pytest.mark.parametrize(
    ("job", "title", "commit"),
    [
        (COMMIT, "Merge the findings of the review of commit 1a2b3c4", "1a2b3c4"),
        (
            BATCH,
            "Merge the findings of the review of the 3 commits made for review batch-2",
            "9f8e7d6",
        ),
        (SUPER, "Merge the findings of the super review at 5e6f7a8", "5e6f7a8"),
        (SET, "Merge the findings of the mutation report at 1a2b3c4", "1a2b3c4"),
    ],
)
def test_reviews_finished_titles_the_merge_by_what_was_reviewed(
    job: ReviewJob, title: str, commit: str
) -> None:
    values = finished_values(job, (), 5, MergeBriefing())
    assert (values["title"], values["commit"]) == (title, commit)


def test_reviews_finished_lists_each_finding_as_a_paragraph_in_id_order() -> None:
    group = reported(ReviewGroup(COMMIT), "bugs-reviewer", BLOCKING)
    group = reported(group, "tests-reviewer", FindingReport("No test for it", "", "normal"))
    assert finished_values(COMMIT, group.findings, 5, MergeBriefing())["findings"] == (
        "F1 · bugs-reviewer · blocking: Crash on empty input\nparse('') raises.\n\n"
        "F2 · tests-reviewer · normal: No test for it"
    )


def test_reviews_finished_gives_the_cap_and_lists_as_the_review_templates_do() -> None:
    values = finished_values(COMMIT, (), 7, MergeBriefing(("Add tests",), ("Add B",)))
    assert values == {
        "title": "Merge the findings of the review of commit 1a2b3c4",
        "commit": "1a2b3c4",
        "findings": "",
        "cap": "7",
        "queued": "Tasks already queued (don't suggest these):\n- Add tests",
        "landed_since": "Landed since (check your findings against these):\n- Add B",
        "messages": "",
    }


def test_reviews_finished_gives_empty_lists_when_nothing_is_queued_or_landed() -> None:
    values = finished_values(SUPER, (), 10, MergeBriefing())
    assert (values["queued"], values["landed_since"]) == ("", "")


def test_reviews_finished_quotes_a_reviewed_commits_message_indented_under_its_commit() -> None:
    briefing = MergeBriefing(messages=("Add the flag\n\nOrbit task:\n\nPlan: docs/x.md",))
    assert finished_values(COMMIT, (), 5, briefing)["messages"] == (
        "Reviewed commit 1a2b3c4:\n    Add the flag\n\n    Orbit task:\n\n    Plan: docs/x.md"
    )


def test_reviews_finished_quotes_each_of_a_batchs_messages_in_commit_order() -> None:
    briefing = MergeBriefing(messages=("Add a", "Add b", "Add c"))
    assert finished_values(BATCH, (), 5, briefing)["messages"] == (
        "Reviewed commit c1:\n    Add a\n\n"
        "Reviewed commit c2:\n    Add b\n\n"
        "Reviewed commit 9f8e7d6:\n    Add c"
    )


def test_reviews_finished_trims_the_messages_past_their_limit() -> None:
    briefing = MergeBriefing(messages=("x" * MESSAGES_LIMIT,))
    messages = finished_values(COMMIT, (), 5, briefing)["messages"]
    assert len(messages) == MESSAGES_LIMIT + len("\n… (messages trimmed)")
    assert messages.endswith("x\n… (messages trimmed)")


@pytest.mark.parametrize("job", [SUPER, SET])
def test_reviews_finished_quotes_no_messages_for_a_review_of_no_task(job: ReviewJob) -> None:
    briefing = MergeBriefing(messages=("Add it",))
    assert finished_values(job, (), 5, briefing)["messages"] == ""


@pytest.mark.parametrize(
    ("job", "quoted"), [(COMMIT, True), (BATCH, True), (SUPER, False), (SET, False)]
)
def test_only_a_review_of_tasks_commits_quotes_their_messages(job: ReviewJob, quoted: bool) -> None:
    assert quotes_messages(job) is quoted
