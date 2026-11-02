"""Keeping the holds on merges: a holding set's failure holds them, with its notice and a fix at
the front of the queue; passing lifts the hold; a fix found through its chain; holds kept in the
store, restored without the sets removed, and a failing save halting the session."""

import asyncio
from collections.abc import Mapping

import pytest

from orbit.agent.application.merge_holds import HoldParts, MergeHolds
from orbit.agent.application.ports import StoreFailed, TreeFailed
from orbit.agent.application.set_runs import SetResult
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.merge_holds import Hold
from orbit.agent.domain.roles import Role
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.agent.domain.verification import CheckSet, VerificationStep
from orbit.protocol import (
    CODER,
    INTERNAL,
    ErrorOccurred,
    Event,
    MergeHold,
    Notice,
    TaskSpec,
    TaskStatus,
)
from tests.agent_fakes import ROLES, FakeHalt, FakeStore, FakeTree

STEP = VerificationStep("Stress tests", ("stress",), "stress")
STRESS = CheckSet("stress", (STEP,), "holds_merges")
SOAK = CheckSet("soak", (VerificationStep("Soak", ("soak",), "soak"),), "holds_merges")
FAILED = (STEP, "2 failed")
HELD = Hold("stress", "c4", "Stress tests", "2 failed", 1)


class Holds:
    """Holds of the sets ``holding`` merges, stress and soak unless given, over ``store``, each
    queuing ``limit`` fixes, recording what they publish, kick, run again, change and
    release."""

    def __init__(
        self,
        store: FakeStore | None = None,
        roles: Mapping[str, Role] = ROLES,
        limit: int = 3,
        holding: tuple[CheckSet, ...] = (STRESS, SOAK),
    ) -> None:
        self.events: list[Event] = []
        self.kicked: list[str] = []
        self.reruns: list[tuple[str, str]] = []
        self.changes = 0
        self.releases = 0
        self.halt = FakeHalt()
        self.tree = FakeTree()
        self.tasks = Tasks(TaskBoard(), self.publish)
        parts = HoldParts(
            self.tasks,
            lambda: roles,
            self.tree,
            self.publish,
            store,
            self.halt,
            self.kicked.append,
            lambda name, trigger: self.reruns.append((name, trigger)),
        )
        self.holds = MergeHolds(holding, limit, parts)
        self.holds.changed = self.change
        self.holds.released = self.release

    async def publish(self, event: Event) -> None:
        self.events.append(event)

    def change(self) -> None:
        self.changes += 1

    def release(self) -> None:
        self.releases += 1

    def notices(self) -> list[str]:
        return [event.message for event in self.events if isinstance(event, Notice)]

    def prompts(self) -> list[str]:
        return [state.task.prompt for state in self.tasks.board.tasks]


async def failed(holds: Holds, commit: str = "c4", check_set: CheckSet = STRESS) -> None:
    await holds.holds.heard(SetResult(check_set, commit, "nightly", FAILED))


async def passed(holds: Holds, commit: str = "c5", check_set: CheckSet = STRESS) -> None:
    await holds.holds.heard(SetResult(check_set, commit, "nightly", None))


async def test_a_failure_holds_merges_with_a_notice_and_a_fix_first_from_its_trigger() -> None:
    store = FakeStore()
    holds = Holds(store)
    await holds.holds.restore()
    holds.tasks.board.add(TaskSpec(prompt="Add a flag."))
    await failed(holds)

    assert holds.holds.held and holds.holds.holds == (HELD,)
    assert holds.holds.views == (
        MergeHold(set="stress", commit="c4", reason="Stress tests failed"),
    )
    assert holds.notices() == ["Merges held: stress failed on c4. A fix is queued first."]
    fix = holds.tasks.board.tasks[0]
    assert (fix.task.role, fix.origin, fix.status) == (CODER, "nightly", "pending")
    assert fix.task.prompt.startswith("Fix the failing set: stress\n\n")
    assert "Its output:\n\n2 failed" in fix.task.prompt
    assert holds.kicked == [CODER]
    assert store.holds_saved == [(HELD,)] and holds.changes == 2


async def test_a_failure_while_its_fix_is_still_to_run_queues_no_other() -> None:
    holds = Holds()
    await failed(holds)
    await failed(holds, "c5")
    assert len(holds.prompts()) == 1
    assert holds.holds.holds == (Hold("stress", "c5", "Stress tests", "2 failed", 1),)


async def test_a_failure_after_its_fix_ran_queues_another() -> None:
    holds = Holds()
    await failed(holds)
    holds.tasks.board.set_status(holds.tasks.board.tasks[0].id, "completed")
    await failed(holds, "c5")
    assert len(holds.prompts()) == 2
    assert holds.holds.holds == (Hold("stress", "c5", "Stress tests", "2 failed", 2),)


@pytest.mark.parametrize("cut_short", ["error", "cancelled"])
async def test_a_failure_while_its_fix_is_cut_short_queues_no_other(cut_short: TaskStatus) -> None:
    holds = Holds()
    await failed(holds)
    holds.tasks.board.set_status(holds.tasks.board.tasks[0].id, cut_short)
    await failed(holds, "c5")
    assert len(holds.prompts()) == 1
    assert holds.holds.holds == (Hold("stress", "c5", "Stress tests", "2 failed", 1),)


@pytest.mark.parametrize("cut_short", ["error", "cancelled"])
async def test_a_fix_ending_unlanded_while_another_is_cut_short_queues_no_other(
    cut_short: TaskStatus,
) -> None:
    holds = Holds()
    board = holds.tasks.board
    await failed(holds)
    stopped = board.tasks[0].id
    board.set_status(stopped, cut_short)
    other = board.add_first(TaskSpec(prompt=board.get(stopped).task.prompt), "nightly")
    board.set_status(other, "gave_up")
    await holds.holds.hear_unlanded_fix(board.get(other))
    assert len(holds.prompts()) == 2 and holds.holds.holds == (HELD,)


async def test_the_fix_lists_the_commits_since_the_set_last_passed() -> None:
    holds = Holds()
    holds.tree.since = {"c2": ["Add A", "Add B"]}
    await passed(holds, "c2")
    await failed(holds)
    assert holds.tree.asked_since == [("c2", "c4")]
    assert "The commits since it last passed:\n\n- Add A\n- Add B" in holds.prompts()[0]
    assert holds.notices() == ["Merges held: stress failed on c4. A fix is queued first."]


async def test_the_fix_lists_no_commits_when_git_cant() -> None:
    holds = Holds()
    holds.tree.failures["landed_since"] = TreeFailed("git log failed")
    await passed(holds, "c2")
    await failed(holds)
    assert "Orbit hasn't seen it pass since it started" in holds.prompts()[0]


async def test_without_a_coder_no_fix_is_queued() -> None:
    holds = Holds(roles={})
    await failed(holds)
    assert holds.holds.held and holds.prompts() == [] and holds.kicked == []


async def test_passing_lifts_the_hold_with_a_notice_and_releases_once_none_is_left() -> None:
    store = FakeStore()
    holds = Holds(store)
    await holds.holds.restore()  # Reading no hold kept releases once.
    await failed(holds)
    await failed(holds, check_set=SOAK)
    await passed(holds)
    assert holds.releases == 1 and [hold.key for hold in holds.holds.holds] == ["soak"]
    await passed(holds, check_set=SOAK)

    assert not holds.holds.held and holds.releases == 2
    assert holds.notices()[-2:] == [
        "Merges resumed: stress passed.",
        "Merges resumed: soak passed.",
    ]
    assert store.holds == ()


async def test_passing_with_no_hold_changes_nothing() -> None:
    store = FakeStore()
    holds = Holds(store)
    await holds.holds.restore()  # Reading no hold kept releases once.
    await passed(holds)
    assert holds.notices() == [] and holds.releases == 1 and store.holds_saved == []


async def test_a_held_sets_fix_is_known_through_its_chain_of_fixes() -> None:
    holds = Holds()
    board = holds.tasks.board
    other = board.add(TaskSpec(prompt="Add a flag."))
    await failed(holds)
    fix = board.tasks[0].id
    carries_on = board.add(TaskSpec(prompt="Fix the failed verification step: Lint"), fixes=fix)

    assert holds.holds.fix_of(board.get(fix)) == "stress"
    assert holds.holds.fix_of(board.get(carries_on)) == "stress"
    assert holds.holds.fix_of(board.get(other)) is None
    await passed(holds)
    assert holds.holds.fix_of(board.get(fix)) is None


def test_a_landed_fix_runs_its_set_again_as_its_trigger() -> None:
    holds = Holds()
    holds.holds.fix_landed("stress", "nightly")
    assert holds.reruns == [("stress", "nightly")]


async def test_a_fix_ending_unlanded_queues_the_next_first_from_its_trigger() -> None:
    holds = Holds()
    board = holds.tasks.board
    await failed(holds)
    fix = board.tasks[0].id
    board.set_status(fix, "gave_up")
    await holds.holds.hear_unlanded_fix(board.get(fix))

    assert holds.holds.holds == (Hold("stress", "c4", "Stress tests", "2 failed", 2),)
    nxt = board.tasks[0]
    assert (nxt.origin, nxt.status) == ("nightly", "pending") and nxt.id != fix
    assert nxt.task.prompt.startswith("Fix the failing set: stress\n\n")
    assert "Its output:\n\n2 failed" in nxt.task.prompt
    assert holds.kicked == [CODER, CODER]


async def test_a_fix_ending_unlanded_while_a_fix_carries_on_from_it_queues_no_other() -> None:
    holds = Holds()
    board = holds.tasks.board
    await failed(holds)
    fix = board.tasks[0].id
    board.set_status(fix, "unverified")
    board.add(TaskSpec(prompt="Fix the failed verification step: Lint"), fixes=fix)
    await holds.holds.hear_unlanded_fix(board.get(fix))
    assert len(holds.prompts()) == 2 and holds.holds.holds == (HELD,)


async def test_a_fix_ending_unlanded_once_the_fixes_ran_out_asks_the_user_to_step_in() -> None:
    holds = Holds(limit=1)
    board = holds.tasks.board
    await failed(holds)
    fix = board.tasks[0].id
    board.set_status(fix, "unverified")
    await holds.holds.hear_unlanded_fix(board.get(fix))
    assert holds.prompts() == [board.get(fix).task.prompt] and holds.holds.holds == (HELD,)
    assert holds.notices()[-1] == (
        "stress still fails after 1 fix; merges stay held. Release with ctrl+f or fix it yourself."
    )


async def test_a_task_ending_unlanded_that_fixes_no_set_held_changes_nothing() -> None:
    holds = Holds()
    board = holds.tasks.board
    other = board.add(TaskSpec(prompt="Add a flag."))
    await failed(holds)
    fix = board.tasks[0].id
    await passed(holds)
    await holds.holds.hear_unlanded_fix(board.get(other))
    await holds.holds.hear_unlanded_fix(board.get(fix))
    assert len(holds.prompts()) == 2 and not holds.holds.held and holds.reruns == []


async def test_a_fix_ending_unlanded_runs_its_set_again_if_its_step_is_gone() -> None:
    store = FakeStore()
    store.holds = (Hold("stress", "c4", "Old step", "2 failed", 1),)
    holds = Holds(store)
    await holds.holds.restore()
    board = holds.tasks.board
    fix = board.add_first(TaskSpec(prompt="Fix the failing set: stress"), "nightly")
    board.set_status(fix, "gave_up")
    await holds.holds.hear_unlanded_fix(board.get(fix))
    assert holds.reruns == [("stress", "nightly")] and len(holds.prompts()) == 1


async def test_holds_kept_are_restored_without_the_sets_removed() -> None:
    store = FakeStore()
    gone = Hold("gone", "c1", "Old", "failed")
    store.holds = (gone, HELD)
    holds = Holds(store)
    await holds.holds.restore()
    assert holds.holds.holds == (HELD,) and holds.changes == 1
    assert store.holds_saved == [(HELD,)]


async def test_holds_kept_as_they_are_are_restored_without_saving() -> None:
    store = FakeStore()
    store.holds = (HELD,)
    holds = Holds(store)
    await holds.holds.restore()
    assert holds.holds.held and store.holds_saved == []


async def test_a_hold_started_before_the_kept_ones_were_read_replaces_its_sets() -> None:
    store = FakeStore()
    store.holds = (Hold("stress", "c1", "Stress tests", "old", 2),)
    holds = Holds(store)
    await failed(holds)
    await holds.holds.restore()
    assert holds.holds.holds == (HELD,)


async def test_without_a_store_nothing_is_restored_or_saved() -> None:
    holds = Holds()
    await holds.holds.restore()
    await failed(holds)
    assert holds.holds.held and await holds.holds.retry() is None


async def test_holds_that_cant_be_read_are_reported_and_none_restored() -> None:
    store = FakeStore()
    store.holds = (HELD,)
    store.holds_load_failure = "disk gone"
    holds = Holds(store)
    await holds.holds.restore()
    assert not holds.holds.held
    assert [event.code for event in holds.events if isinstance(event, ErrorOccurred)] == [INTERNAL]


async def test_holds_that_cant_be_read_are_kept_through_holds_starting_and_lifting() -> None:
    store = FakeStore()
    store.holds = (HELD,)
    store.holds_load_failure = "disk gone"
    holds = Holds(store)
    await holds.holds.restore()
    store.holds_load_failure = None
    await failed(holds, check_set=SOAK)
    await passed(holds, check_set=SOAK)
    await failed(holds, check_set=SOAK)
    assert await holds.holds.retry() is None
    assert store.holds_saved == [] and store.holds == (HELD,)


async def test_holds_read_at_last_take_on_those_started_since_and_save_them() -> None:
    store = FakeStore()
    store.holds = (HELD,)
    store.holds_load_failure = "disk gone"
    holds = Holds(store)
    await holds.holds.restore()
    await failed(holds, check_set=SOAK)
    store.holds_load_failure = None
    await holds.holds.restore()
    soaked = Hold("soak", "c4", "Stress tests", "2 failed", 1)
    assert holds.holds.holds == (HELD, soaked) and store.holds_saved == [(HELD, soaked)]


async def test_holds_read_again_take_on_those_changed_elsewhere_and_keep_those_started() -> None:
    store = FakeStore()
    store.holds = (HELD,)
    holds = Holds(store)
    await holds.holds.restore()
    store.holds_load_failure = "disk gone"
    await holds.holds.restore()
    await failed(holds, check_set=SOAK)
    elsewhere = Hold("stress", "c7", "Stress tests", "3 failed", 2)
    store.holds, store.holds_load_failure = (elsewhere,), None
    await holds.holds.restore()
    soaked = Hold("soak", "c4", "Stress tests", "2 failed", 1)
    assert holds.holds.holds == (elsewhere, soaked) and store.holds_saved == [(elsewhere, soaked)]


async def test_holds_read_again_with_none_left_release_the_work_waiting() -> None:
    store = FakeStore()
    store.holds = (HELD,)
    holds = Holds(store)
    await holds.holds.restore()
    store.holds = ()
    await holds.holds.restore()
    assert not holds.holds.held and holds.releases == 1 and store.holds_saved == []


async def test_holds_read_with_none_kept_release_the_work_saved_waiting() -> None:
    holds = Holds(FakeStore())
    assert not holds.holds.is_known
    await holds.holds.restore()
    assert holds.holds.is_known and holds.releases == 1


async def test_holds_that_cant_be_read_are_unknown_and_release_nothing() -> None:
    store = FakeStore()
    store.holds_load_failure = "disk gone"
    holds = Holds(store)
    await holds.holds.restore()
    assert not holds.holds.is_known and holds.releases == 0


async def test_holds_released_by_hand_though_unread_are_known() -> None:
    store = FakeStore()
    store.holds_load_failure = "disk gone"
    holds = Holds(store)
    await holds.holds.restore()
    await failed(holds)
    assert not holds.holds.is_known
    await holds.holds.release()
    assert holds.holds.is_known and holds.releases == 1


async def test_the_last_holding_set_passing_unread_with_no_hold_on_releases_once() -> None:
    store = FakeStore()
    store.holds_load_failure = "disk gone"
    holds = Holds(store, holding=(STRESS,))
    await holds.holds.restore()
    await passed(holds)
    await passed(holds, "c6")
    assert holds.holds.is_known and holds.releases == 1 and store.holds_saved == []


async def test_one_of_two_holding_sets_passing_unread_releases_only_with_the_other() -> None:
    store = FakeStore()
    store.holds_load_failure = "disk gone"
    holds = Holds(store)
    await holds.holds.restore()
    await passed(holds)
    assert not holds.holds.is_known and holds.releases == 0
    await passed(holds, check_set=SOAK)
    assert holds.holds.is_known and holds.releases == 1 and store.holds_saved == []


async def test_the_last_holding_set_lifting_its_hold_though_unread_releases_once() -> None:
    store = FakeStore()
    store.holds_load_failure = "disk gone"
    holds = Holds(store, holding=(STRESS,))
    await holds.holds.restore()
    await failed(holds)
    await passed(holds)
    assert holds.holds.is_known and holds.releases == 1 and store.holds_saved == []


async def test_holds_without_a_store_are_known() -> None:
    assert Holds().holds.is_known


async def test_holds_read_still_held_release_nothing() -> None:
    store = FakeStore()
    store.holds = (HELD,)
    holds = Holds(store)
    await holds.holds.restore()
    assert holds.holds.held and holds.releases == 0


async def test_holds_read_again_without_the_store_are_kept_and_nothing_saved() -> None:
    store = FakeStore()
    holds = Holds(store)
    await holds.holds.restore()
    await failed(holds)
    store.holds_load_failure = "disk gone"
    await holds.holds.restore()
    await passed(holds)
    assert not holds.holds.held and store.holds == (HELD,)


async def test_a_set_passing_while_the_holds_kept_cant_be_read_takes_on_none_of_its_after() -> None:
    store = FakeStore()
    store.holds = (HELD,)
    store.holds_load_failure = "disk gone"
    holds = Holds(store)
    await holds.holds.restore()
    await passed(holds)
    store.holds_load_failure = None
    await holds.holds.restore()
    assert not holds.holds.held and store.holds_saved == [()] and holds.releases == 1


async def test_a_hold_lifted_while_the_store_cant_be_read_stays_lifted_once_it_can() -> None:
    store = FakeStore()
    holds = Holds(store)
    await holds.holds.restore()
    await failed(holds)
    store.holds_load_failure = "disk gone"
    await holds.holds.restore()
    await passed(holds)
    store.holds_load_failure = None
    await holds.holds.restore()
    assert not holds.holds.held and store.holds == () and holds.releases == 3


async def test_a_set_failing_again_after_its_lift_before_the_read_keeps_its_new_hold() -> None:
    store = FakeStore()
    store.holds = (HELD,)
    store.holds_load_failure = "disk gone"
    holds = Holds(store)
    await holds.holds.restore()
    await failed(holds)
    await passed(holds)
    await failed(holds, commit="c6")
    store.holds_load_failure = None
    await holds.holds.restore()
    assert holds.holds.holds == (Hold("stress", "c6", "Stress tests", "2 failed"),)  # Fixing.
    assert store.holds == holds.holds.holds


async def test_holds_released_by_hand_while_unread_are_none_of_them_taken_on_after() -> None:
    store = FakeStore()
    soaked = Hold("soak", "c2", "Soak", "timed out", 1)
    store.holds = (HELD, soaked)
    holds = Holds(store)
    await holds.holds.restore()
    store.holds_load_failure = "disk gone"
    await holds.holds.restore()
    await holds.holds.release()
    store.holds_load_failure = None
    await holds.holds.restore()
    assert not holds.holds.held and store.holds_saved == [()]


async def test_a_set_passing_during_the_takeover_read_keeps_no_hold() -> None:
    store = FakeStore()
    store.holds = (HELD,)
    holds = Holds(store)
    await holds.holds.restore()
    reading = asyncio.create_task(holds.holds.restore())
    await asyncio.sleep(0)  # The read begins.
    await passed(holds)
    await reading
    assert not holds.holds.held and not store.holds


async def test_a_hold_lifted_and_saved_then_put_back_elsewhere_is_taken_on_from_the_read() -> None:
    store = FakeStore()
    holds = Holds(store)
    await holds.holds.restore()
    await failed(holds)
    await passed(holds)
    await passed(holds, check_set=SOAK)
    store.holds = (HELD,)
    await holds.holds.restore()
    assert holds.holds.holds == (HELD,)


async def test_a_hold_lifted_during_a_save_that_saves_it_not_is_dropped_from_the_read() -> None:
    store = FakeStore()
    store.holds = (HELD,)
    holds = Holds(store)
    await holds.holds.restore()
    saving = asyncio.create_task(failed(holds, check_set=SOAK))
    lifting = asyncio.create_task(passed(holds))
    await asyncio.sleep(0)  # The save of soak's hold begins, and the lift waits for it.
    store.holds_failure = "disk full"
    await asyncio.gather(saving, lifting)
    store.holds_failure = None
    await holds.holds.restore()
    assert [hold.key for hold in store.holds] == ["stress", "soak"]
    assert [hold.key for hold in holds.holds.holds] == ["soak"]


async def test_a_failing_save_halts_the_session_and_a_retry_saves_the_holds() -> None:
    store = FakeStore()
    store.holds_failure = "disk full"
    holds = Holds(store)
    await holds.holds.restore()
    await failed(holds)
    assert holds.halt.failures == [(FailedWrite.MERGE_HOLDS, "disk full")]
    assert isinstance(await holds.holds.retry(), StoreFailed)

    store.holds_failure = None
    assert await holds.holds.retry() is None
    assert store.holds == (HELD,)


async def test_releasing_by_hand_lifts_every_hold_with_a_notice_saved_and_told() -> None:
    store = FakeStore()
    holds = Holds(store)
    await holds.holds.restore()
    await failed(holds)
    await failed(holds, check_set=SOAK)
    await holds.holds.release()
    assert not holds.holds.held and holds.releases == 2  # Once as no hold kept was read.
    assert holds.notices()[-1] == "Merges resumed by hand; stress, soak haven't passed."
    assert store.holds_saved[-1] == ()


async def test_releasing_by_hand_with_no_hold_on_does_nothing() -> None:
    holds = Holds()
    await holds.holds.release()
    assert holds.events == [] and holds.releases == 0 and holds.changes == 0


# Sets given anew


async def test_a_set_that_comes_to_hold_merges_holds_them_as_it_next_fails() -> None:
    holds = Holds(holding=())
    await holds.holds.use_sets((STRESS,))
    await failed(holds)
    assert holds.holds.holds == (HELD,)

    board = holds.tasks.board
    board.set_status(board.tasks[0].id, "gave_up")
    await holds.holds.hear_unlanded_fix(board.tasks[0])
    assert len(holds.prompts()) == 2 and holds.reruns == []


async def test_the_hold_of_a_set_that_no_longer_holds_merges_is_dropped_saved_and_told() -> None:
    store = FakeStore()
    holds = Holds(store)
    await holds.holds.restore()
    await failed(holds)
    await failed(holds, check_set=SOAK)
    soak = holds.holds.holds[1]

    await holds.holds.use_sets((SOAK,))
    assert holds.holds.holds == (soak,) and store.holds_saved[-1] == (soak,)
    assert holds.releases == 1  # Only as no hold kept was read.

    await holds.holds.use_sets(())
    assert not holds.holds.held and store.holds == () and holds.releases == 2


async def test_sets_given_anew_that_drop_no_hold_change_nothing() -> None:
    holds = Holds()
    await failed(holds)
    changes = holds.changes
    await holds.holds.use_sets((STRESS,))
    assert holds.holds.holds == (HELD,) and holds.changes == changes and holds.releases == 0
