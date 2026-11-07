"""The rules of merge holds: what a set's failure does to the holds on, the fixes it queues up to
the limit, lifting and restoring holds, the notices, the fix's prompt and title, and finding the
commits of work that landed stacked."""

import pytest

from orbit.agent.domain.commits import Commit
from orbit.agent.domain.merge_holds import (
    NONE_LIFTED,
    Hold,
    HoldDecision,
    decide_failure,
    decide_unlanded_fix,
    find_fix_root,
    find_fixed_set,
    find_hold,
    fold_holds,
    held_step,
    holding_sets,
    keep_holds,
    lift_hold,
    merge_holds,
    note_held,
    note_lift,
    set_fix_prompt,
    stacked_commits,
    title_fix,
    view_hold,
    word_resumed,
    word_step_in,
)
from orbit.agent.domain.verification import CheckSet, VerificationStep
from orbit.protocol import MergeHold, TaskSpec, TaskState

STRESS = Hold("stress", "1a2b3c4d5e", "Stress tests", "2 failed")
SOAK = Hold("soak", "9f8e7d6c", "Soak", "timed out")


def test_the_notices_name_the_set_its_short_commit_and_the_fixes_tried() -> None:
    assert note_held("stress", "1a2b3c4d5e") == (
        "Merges held: stress failed on 1a2b3c4. A fix is queued first."
    )
    assert word_resumed("stress") == "Merges resumed: stress passed."
    assert word_step_in("stress", 3) == (
        "stress still fails after 3 fixes; merges stay held. Release with ctrl+f or fix it"
        " yourself."
    )
    assert word_step_in("stress", 1).startswith("stress still fails after 1 fix;")


def test_a_first_failure_holds_merges_with_its_notice_and_queues_the_first_fix() -> None:
    assert decide_failure((), STRESS, 3, fixing=False) == HoldDecision(
        (Hold("stress", "1a2b3c4d5e", "Stress tests", "2 failed", 1),),
        (note_held("stress", "1a2b3c4d5e"),),
        fix=True,
    )


def test_a_failure_again_keeps_the_hold_with_it_and_queues_another_fix() -> None:
    held = (Hold("stress", "old", "Stress tests", "1 failed", 1),)
    again = Hold("stress", "new", "Stress tests", "3 failed")
    assert decide_failure(held, again, 3, fixing=False) == HoldDecision(
        (Hold("stress", "new", "Stress tests", "3 failed", 2),), fix=True
    )


def test_a_failure_once_the_fixes_ran_out_asks_the_user_to_step_in() -> None:
    held = (Hold("stress", "old", "Stress tests", "1 failed", 3),)
    again = Hold("stress", "new", "Stress tests", "3 failed")
    assert decide_failure(held, again, 3, fixing=False) == HoldDecision(
        (Hold("stress", "new", "Stress tests", "3 failed", 3),), (word_step_in("stress", 3),)
    )


def test_a_failure_while_a_fix_is_still_to_land_queues_no_other() -> None:
    held = (Hold("stress", "old", "Stress tests", "1 failed", 1),)
    again = Hold("stress", "new", "Soak", "3 failed")
    assert decide_failure(held, again, 3, fixing=True) == HoldDecision(
        (Hold("stress", "new", "Soak", "3 failed", 1),)
    )


def test_a_first_failure_while_its_fix_is_queued_already_holds_without_another() -> None:
    assert decide_failure((), STRESS, 3, fixing=True) == HoldDecision(
        (STRESS,), (note_held("stress", "1a2b3c4d5e"),)
    )


def test_with_no_fixes_allowed_a_first_failure_holds_and_asks_the_user_to_step_in() -> None:
    assert decide_failure((), STRESS, 0, fixing=False).notes == (
        note_held("stress", "1a2b3c4d5e"),
        word_step_in("stress", 0),
    )


def test_a_fix_ending_unlanded_is_its_holds_failure_again_queuing_another_fix() -> None:
    held = (Hold("stress", "c4", "Stress tests", "2 failed", 1), SOAK)
    assert decide_unlanded_fix(held, "stress", 3, fixing=False) == HoldDecision(
        (SOAK, Hold("stress", "c4", "Stress tests", "2 failed", 2)), fix=True
    )


def test_a_fix_ending_unlanded_once_the_fixes_ran_out_asks_the_user_to_step_in() -> None:
    held = (Hold("stress", "c4", "Stress tests", "2 failed", 3),)
    assert decide_unlanded_fix(held, "stress", 3, fixing=False) == HoldDecision(
        held, (word_step_in("stress", 3),)
    )


def test_a_fix_ending_unlanded_while_another_carries_on_changes_nothing() -> None:
    held = (Hold("stress", "c4", "Stress tests", "2 failed", 1), SOAK)
    assert decide_unlanded_fix(held, "stress", 3, fixing=True) == HoldDecision(held)


def test_a_fix_ending_unlanded_for_a_set_holding_none_changes_nothing() -> None:
    assert decide_unlanded_fix((SOAK,), "stress", 3, fixing=False) == HoldDecision((SOAK,))


def test_each_set_holds_on_its_own() -> None:
    decision = decide_failure((SOAK,), STRESS, 3, fixing=False)
    assert decision.holds == (SOAK, Hold("stress", "1a2b3c4d5e", "Stress tests", "2 failed", 1))
    assert find_hold(decision.holds, "soak") == SOAK
    assert find_hold(decision.holds, "lint") is None
    assert lift_hold(decision.holds, "stress") == (SOAK,)


def test_restored_holds_keep_only_the_sets_still_holding_merges() -> None:
    assert keep_holds((STRESS, SOAK), {"soak", "lint"}) == (SOAK,)


def test_holds_started_before_the_kept_ones_were_read_replace_their_sets() -> None:
    newer = Hold("stress", "new", "Stress tests", "3 failed")
    assert merge_holds((STRESS, SOAK), (newer,)) == (SOAK, newer)


def test_holds_read_drop_the_sets_lifted_since_and_take_on_those_started() -> None:
    newer = Hold("stress", "new", "Stress tests", "3 failed")
    lifted = note_lift(note_lift(NONE_LIFTED, "stress"), "soak")
    assert fold_holds((STRESS, SOAK), {"stress", "soak"}, lifted, (newer,)) == (newer,)
    soaked = note_lift(NONE_LIFTED, "soak")
    assert fold_holds((STRESS, SOAK), {"stress", "soak"}, soaked, ()) == (STRESS,)


def test_holds_read_drop_every_kept_one_once_every_hold_was_released() -> None:
    newer = Hold("stress", "new", "Stress tests", "3 failed")
    assert note_lift("every", "stress") == "every"
    assert fold_holds((STRESS, SOAK), {"stress", "soak"}, "every", (newer,)) == (newer,)


def test_holds_read_with_none_lifted_keep_the_sets_still_holding_merges() -> None:
    assert fold_holds((STRESS, SOAK), {"soak"}, NONE_LIFTED, ()) == (SOAK,)


def test_the_sets_holding_merges_are_those_whose_failure_does() -> None:
    stress = CheckSet("stress", (), "holds_merges")
    assert holding_sets((stress, CheckSet("nightly", ()))) == (stress,)


def test_a_holds_step_is_found_in_its_set_by_name() -> None:
    step = VerificationStep("Stress tests", ("stress",), "stress")
    sets = (
        CheckSet("soak", (VerificationStep("Stress tests", ("soak",), "soak"),), "holds_merges"),
        CheckSet("stress", (VerificationStep("Lint", ("lint",), "stress"), step), "holds_merges"),
    )
    assert held_step(sets, STRESS) == step


def test_a_holds_step_gone_from_its_set_is_found_nowhere() -> None:
    sets = (CheckSet("stress", (VerificationStep("Lint", ("lint",), "stress"),), "holds_merges"),)
    assert held_step(sets, STRESS) is None
    assert held_step((), STRESS) is None


def test_a_hold_shows_its_set_commit_and_failed_step() -> None:
    assert view_hold(STRESS) == MergeHold(
        set="stress", commit="1a2b3c4d5e", reason="Stress tests failed"
    )


def test_the_fix_prompt_lists_the_commits_since_the_set_passed_and_its_output() -> None:
    prompt = set_fix_prompt(STRESS, ["Add A", "Add B"], "REPORT")
    assert prompt.partition("\n")[0] == title_fix("stress") == "Fix the failing set: stress"
    assert "The set stress failed on 1a2b3c4 at its step Stress tests." in prompt
    assert "The commits since it last passed:\n\n- Add A\n- Add B\n\nREPORT\n\n" in prompt


def test_the_fix_prompt_says_when_nothing_landed_or_it_never_passed() -> None:
    assert "No commit landed since it last passed." in set_fix_prompt(STRESS, [], "R")
    assert "Orbit hasn't seen it pass since it started" in set_fix_prompt(STRESS, None, "R")


def test_the_set_a_task_fixes_is_named_in_its_title() -> None:
    assert find_fixed_set(set_fix_prompt(STRESS, None, "R")) == "stress"
    assert find_fixed_set("Add a flag.\n\nFix the failing set: stress") is None


def task(task_id: str, fixes: str | None = None) -> TaskState:
    return TaskState(id=task_id, task=TaskSpec(prompt=task_id), fixes=fixes)


def test_a_chain_of_fixes_starts_from_the_first_task_found() -> None:
    tasks = {state.id: state for state in (task("1"), task("2", "1"), task("3", "2"))}
    assert find_fix_root(tasks["3"], tasks.get).id == "1"
    assert find_fix_root(task("4", "gone"), tasks.get).id == "4"


def history(*subjects: str) -> tuple[Commit, ...]:
    """Commits with ``subjects``, oldest first, each going by its subject in lower case."""
    return tuple(Commit(subject.lower(), subject) for subject in subjects)


STACKED_CASES = [
    # (the commits from the base, oldest first; each piece's subjects; each piece's last commit)
    (history("A1", "A2", "B1", "F1"), (("A1", "A2"), ("B1",), ("F1",)), ("a2", "b1", "f1")),
    (history("U1", "A1", "B1", "F1"), (("A1",), ("B1",), ("F1",)), ("a1", "b1", "f1")),
    (history("A1", "X1", "B1", "G1"), (("A1",), ("B1",), ("G1",)), ("a1", "b1", "g1")),
    (history("A1", "X1", "X2", "B1"), (("A1",), ("B1",)), ("a1", "b1")),
    (history("A1", "B1"), (("A1", "A2"), ("B1",)), ("a1", "b1")),
    (history("A2", "B1"), (("A1", "A2"), ("B1",)), ("a2", "b1")),
    (history("A1", "A2"), (("A1", "A2"), ("B1",)), ("a2", None)),
    (history("A1", "C1"), (("A1",), ("B1",), ("C1",)), ("a1", None, "c1")),
    (history("B1", "F1"), (("A1", "A2"), ("B1",), ("F1",)), (None, "b1", "f1")),
    (history("A1", "B1"), ((), ("B1",)), ("b1", "b1")),
    ((), (("A1",), ()), (None, None)),
]


@pytest.mark.parametrize(("commits", "pieces", "found"), STACKED_CASES)
def test_stacked_work_finds_each_piece_by_its_subjects_from_the_end(
    commits: tuple[Commit, ...],
    pieces: tuple[tuple[str, ...], ...],
    found: tuple[str | None, ...],
) -> None:
    assert stacked_commits(commits, pieces) == found


def test_stacked_work_whose_subjects_repeat_takes_as_many_commits_as_each_piece_made() -> None:
    commits = (Commit("a", "Tidy"), Commit("b", "Tidy"), Commit("c", "Tidy"))
    assert stacked_commits(commits, (("Tidy",), ("Tidy", "Tidy"))) == ("a", "c")
