"""The rules of merge holds: what a set's failure does to the holds on, the fixes it queues up to
the limit, lifting and restoring holds, the notices, the fix's prompt and title, and finding the
commits of work that landed stacked."""

from orbit.agent.domain.merge_holds import (
    Hold,
    HoldDecision,
    decide_failure,
    find_hold,
    fix_root,
    fix_title,
    fixed_set,
    held_note,
    hold_view,
    holding_sets,
    kept_holds,
    lift_hold,
    merge_holds,
    resumed_note,
    set_fix_prompt,
    stacked_commits,
    step_in_note,
)
from orbit.agent.domain.verification import CheckSet
from orbit.protocol import MergeHold, TaskSpec, TaskState

STRESS = Hold("stress", "1a2b3c4d5e", "Stress tests", "2 failed")
SOAK = Hold("soak", "9f8e7d6c", "Soak", "timed out")


def test_the_notices_name_the_set_its_short_commit_and_the_fixes_tried() -> None:
    assert held_note("stress", "1a2b3c4d5e") == (
        "Merges held: stress failed on 1a2b3c4. A fix is queued first."
    )
    assert resumed_note("stress") == "Merges resumed: stress passed."
    assert step_in_note("stress", 3) == (
        "stress still fails after 3 fixes; merges stay held. Release with ctrl+f or fix it"
        " yourself."
    )
    assert step_in_note("stress", 1).startswith("stress still fails after 1 fix;")


def test_a_first_failure_holds_merges_with_its_notice_and_queues_the_first_fix() -> None:
    assert decide_failure((), STRESS, 3, fixing=False) == HoldDecision(
        (Hold("stress", "1a2b3c4d5e", "Stress tests", "2 failed", 1),),
        (held_note("stress", "1a2b3c4d5e"),),
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
        (Hold("stress", "new", "Stress tests", "3 failed", 3),), (step_in_note("stress", 3),)
    )


def test_a_failure_while_a_fix_is_still_to_land_queues_no_other() -> None:
    held = (Hold("stress", "old", "Stress tests", "1 failed", 1),)
    again = Hold("stress", "new", "Soak", "3 failed")
    assert decide_failure(held, again, 3, fixing=True) == HoldDecision(
        (Hold("stress", "new", "Soak", "3 failed", 1),)
    )


def test_a_first_failure_while_its_fix_is_queued_already_holds_without_another() -> None:
    assert decide_failure((), STRESS, 3, fixing=True) == HoldDecision(
        (STRESS,), (held_note("stress", "1a2b3c4d5e"),)
    )


def test_with_no_fixes_allowed_a_first_failure_holds_and_asks_the_user_to_step_in() -> None:
    assert decide_failure((), STRESS, 0, fixing=False).notes == (
        held_note("stress", "1a2b3c4d5e"),
        step_in_note("stress", 0),
    )


def test_each_set_holds_on_its_own() -> None:
    decision = decide_failure((SOAK,), STRESS, 3, fixing=False)
    assert decision.holds == (SOAK, Hold("stress", "1a2b3c4d5e", "Stress tests", "2 failed", 1))
    assert find_hold(decision.holds, "soak") == SOAK
    assert find_hold(decision.holds, "lint") is None
    assert lift_hold(decision.holds, "stress") == (SOAK,)


def test_restored_holds_keep_only_the_sets_still_holding_merges() -> None:
    assert kept_holds((STRESS, SOAK), {"soak", "lint"}) == (SOAK,)


def test_holds_started_before_the_kept_ones_were_read_replace_their_sets() -> None:
    newer = Hold("stress", "new", "Stress tests", "3 failed")
    assert merge_holds((STRESS, SOAK), (newer,)) == (SOAK, newer)


def test_the_sets_holding_merges_are_those_whose_failure_does() -> None:
    sets = (CheckSet("stress", (), "holds_merges"), CheckSet("nightly", ()))
    assert holding_sets(sets) == ("stress",)


def test_a_hold_shows_its_set_commit_and_failed_step() -> None:
    assert hold_view(STRESS) == MergeHold(
        set="stress", commit="1a2b3c4d5e", reason="Stress tests failed"
    )


def test_the_fix_prompt_lists_the_commits_since_the_set_passed_and_its_output() -> None:
    prompt = set_fix_prompt(STRESS, ["Add A", "Add B"], "REPORT")
    assert prompt.partition("\n")[0] == fix_title("stress") == "Fix the failing set: stress"
    assert "The set stress failed on 1a2b3c4 at its step Stress tests." in prompt
    assert "The commits since it last passed:\n\n- Add A\n- Add B\n\nREPORT\n\n" in prompt


def test_the_fix_prompt_says_when_nothing_landed_or_it_never_passed() -> None:
    assert "No commit landed since it last passed." in set_fix_prompt(STRESS, [], "R")
    assert "Orbit hasn't seen it pass since it started" in set_fix_prompt(STRESS, None, "R")


def test_the_set_a_task_fixes_is_named_in_its_title() -> None:
    assert fixed_set(set_fix_prompt(STRESS, None, "R")) == "stress"
    assert fixed_set("Add a flag.\n\nFix the failing set: stress") is None


def task(task_id: str, fixes: str | None = None) -> TaskState:
    return TaskState(id=task_id, task=TaskSpec(prompt=task_id), fixes=fixes)


def test_a_chain_of_fixes_starts_from_the_first_task_found() -> None:
    tasks = {state.id: state for state in (task("1"), task("2", "1"), task("3", "2"))}
    assert fix_root(tasks["3"], tasks.get).id == "1"
    assert fix_root(task("4", "gone"), tasks.get).id == "4"


def test_stacked_work_finds_the_last_commit_of_each_piece_at_the_end() -> None:
    commits = ["u1", "a1", "a2", "b1", "f1"]
    assert stacked_commits(commits, (2, 1, 1)) == ("a2", "b1", "f1")


def test_stacked_work_whose_commits_are_missing_or_none_has_no_commit() -> None:
    assert stacked_commits(["b1", "f1"], (2, 1, 1)) == (None, "b1", "f1")
    assert stacked_commits(["a1", "f1"], (1, 0, 1)) == ("a1", None, "f1")
