"""The rules of a blocking finding's hold on merges: filing it holds them, filing it again from
its fix's review fixes it again up to the limit, the end of that review lifts it or leaves it
unchecked, and how it's restored, shown and told of."""

from dataclasses import replace

from orbit.agent.domain.merge_holds import (
    BLOCKING_REASON,
    NONE_LIFTED,
    BlockingTask,
    FindingHold,
    Hold,
    HoldDecision,
    decide_blocking,
    decide_failure,
    decide_filing,
    decide_fix_review,
    decide_unreviewed,
    find_hold,
    finding_held_note,
    finding_holds,
    finding_resumed_note,
    finding_step_in_note,
    fold_holds,
    hold_view,
    kept_holds,
    note_lift,
    released_note,
    set_holds,
    unchecked_note,
    unfixed,
)
from orbit.protocol import BUGS_REVIEWER, MergeHold

STRESS = Hold("stress", "1a2b3c4d5e", "Stress tests", "2 failed", 1)
CRASH = BlockingTask(BUGS_REVIEWER, "1a2b3c4d5e", "Fix the crash", None)
"""A blocking task filed from a commit's review."""

HELD = FindingHold(BUGS_REVIEWER, "1a2b3c4d5e", "Fix the crash", "9", "batch-2")
"""The hold :data:`CRASH` starts, filed as task 9 in batch-2."""

AGAIN = BlockingTask(BUGS_REVIEWER, "c9", "Fix the crash again", "batch-2")
"""A blocking task from the bugs lens, filed by the review of :data:`HELD`'s fix."""


def test_the_notices_name_the_lens_the_short_commit_the_title_and_the_fixes_tried() -> None:
    assert finding_held_note(HELD) == (
        "Merges held: a blocking finding from bugs-reviewer in 1a2b3c4: Fix the crash. Its fix is"
        " queued first."
    )
    assert finding_resumed_note(BUGS_REVIEWER) == (
        "Merges resumed: the finding from bugs-reviewer is fixed."
    )
    assert finding_step_in_note(BUGS_REVIEWER, 3) == (
        "The finding from bugs-reviewer still holds after 3 fixes; merges stay held. Release with"
        " ctrl+f or fix it yourself."
    )
    assert unchecked_note(BUGS_REVIEWER) == (
        "The finding from bugs-reviewer can't be checked by a review; merges stay held. Release"
        " with ctrl+f or fix it yourself."
    )


def test_a_release_by_hand_names_the_sets_not_passed_and_the_findings_not_fixed() -> None:
    assert unfixed(("security-reviewer", BUGS_REVIEWER)) == (
        "the findings from security-reviewer, bugs-reviewer aren't fixed"
    )
    assert released_note((), (BUGS_REVIEWER,)) == (
        "Merges resumed by hand; the finding from bugs-reviewer isn't fixed."
    )
    assert released_note(("stress",), (BUGS_REVIEWER,)) == (
        "Merges resumed by hand; stress hasn't passed and the finding from bugs-reviewer isn't"
        " fixed."
    )


def test_a_blocking_task_filed_holds_merges_with_its_notice() -> None:
    assert decide_filing((STRESS,), CRASH, 3) == "held"
    assert decide_blocking((STRESS,), CRASH, "9", "batch-2", 3) == HoldDecision(
        (STRESS, HELD), (finding_held_note(HELD),)
    )


def test_a_blocking_task_from_its_fixs_review_by_its_lens_fixes_the_hold_again() -> None:
    assert decide_filing((HELD,), AGAIN, 3) == "fixed_again"
    again = replace(HELD, task="12", batch="batch-4", fixes=2)
    assert decide_blocking((HELD, STRESS), AGAIN, "12", "batch-4", 3) == HoldDecision(
        (STRESS, again)
    )


def test_a_blocking_task_from_another_lens_or_review_starts_a_hold_of_its_own() -> None:
    security = replace(AGAIN, lens="security-reviewer")
    elsewhere = replace(AGAIN, reviewed="batch-7")
    unchecked = (replace(HELD, batch=None),)
    assert decide_filing((HELD,), security, 3) == "held"
    assert decide_filing((HELD,), elsewhere, 3) == "held"
    assert decide_filing(unchecked, replace(AGAIN, reviewed=None), 3) == "held"


def test_a_blocking_task_filed_again_once_the_fixes_ran_out_asks_the_user_to_step_in() -> None:
    spent = replace(HELD, fixes=3)
    assert decide_filing((spent,), AGAIN, 3) == "stepped_in"
    assert decide_blocking((spent,), AGAIN, "12", "batch-1", 3) == HoldDecision(
        (replace(spent, batch=None),), (finding_step_in_note(BUGS_REVIEWER, 3),)
    )


def test_its_fixs_review_with_its_lens_completed_lifts_the_hold_with_its_notice() -> None:
    other = replace(HELD, lens="security-reviewer", title="Escape it", batch="batch-5")
    decision = decide_fix_review((STRESS, HELD, other), "batch-2", {BUGS_REVIEWER, "tests"})
    assert decision == HoldDecision((STRESS, other), (finding_resumed_note(BUGS_REVIEWER),))


def test_its_fixs_review_without_its_lens_completed_leaves_the_hold_unchecked() -> None:
    decision = decide_fix_review((HELD, STRESS), "batch-2", {"tests-reviewer"})
    assert decision == HoldDecision(
        (replace(HELD, batch=None), STRESS), (unchecked_note(BUGS_REVIEWER),)
    )


def test_a_review_of_another_batch_changes_no_hold() -> None:
    assert decide_fix_review((HELD,), "batch-3", {BUGS_REVIEWER}) == HoldDecision((HELD,))


def test_its_fixs_batch_closing_unreviewed_leaves_the_hold_unchecked() -> None:
    assert decide_unreviewed((STRESS, HELD), (HELD,)) == HoldDecision(
        (STRESS, replace(HELD, batch=None)), (unchecked_note(BUGS_REVIEWER),)
    )


def test_finding_holds_sit_beside_set_holds_and_a_sets_failure_leaves_them() -> None:
    decision = decide_failure((HELD,), Hold("soak", "c2", "Soak", "timed out"), 3, fixing=False)
    assert finding_holds(decision.holds) == (HELD,)
    assert [hold.key for hold in set_holds(decision.holds)] == ["soak"]
    assert find_hold((HELD,), BUGS_REVIEWER) is None


def test_a_finding_holds_key_stays_through_its_fixes() -> None:
    assert replace(HELD, task="12", batch="batch-4", fixes=2).key == HELD.key
    assert HELD.key != replace(HELD, title="Fix the leak").key


def test_restored_finding_holds_are_kept_whichever_sets_hold_merges() -> None:
    assert kept_holds((STRESS, HELD), set()) == (HELD,)


def test_finding_holds_read_drop_those_lifted_since_and_take_on_those_started() -> None:
    newer = replace(HELD, fixes=2)
    assert fold_holds((HELD,), set(), NONE_LIFTED, (newer,)) == (newer,)
    assert fold_holds((STRESS, HELD), {"stress"}, note_lift(NONE_LIFTED, HELD.key), ()) == (STRESS,)
    assert fold_holds((HELD,), set(), "every", ()) == ()


def test_a_finding_hold_shows_its_lens_commit_and_title() -> None:
    assert BLOCKING_REASON == "blocking finding"
    assert hold_view(HELD) == MergeHold(
        set=BUGS_REVIEWER,
        commit="1a2b3c4d5e",
        reason="blocking finding",
        finding="Fix the crash",
    )
