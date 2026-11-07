"""Keeping blocking findings' holds on merges: filing one holds merges with its notice, its fix
and the fixes carrying on from it land despite the holds, the end of its fix's review lifts it or
leaves it unchecked, releasing by hand lifts it beside the sets', and it's kept in the store and
restored whichever sets hold merges."""

from orbit.agent.application.merge_holds import HoldParts, MergeHolds
from orbit.agent.application.set_runs import SetResult
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.merge_holds import (
    BlockingTask,
    FindingHold,
    FixReview,
    finding_resumed_note,
    note_finding_held,
    unchecked_note,
)
from orbit.agent.domain.verification import CheckSet, VerificationStep
from orbit.protocol import BUGS_REVIEWER, CODER, Event, MergeHold, Notice, TaskSpec
from tests.agent_fakes import ROLES, FakeHalt, FakeStore, FakeTree

STEP = VerificationStep("Stress tests", ("stress",), "stress")
STRESS = CheckSet("stress", (STEP,), "holds_merges")
CRASH = BlockingTask(BUGS_REVIEWER, "1a2b3c4d", "Fix the crash", None)
HELD = FindingHold(BUGS_REVIEWER, "1a2b3c4d", "Fix the crash", "1", "batch-2")
"""The hold :data:`CRASH`, filed as task 1 in batch-2, starts."""


class Holds:
    """Holds of the set stress over ``store``, each queuing three fixes, recording what they
    publish and how often they release."""

    def __init__(self, store: FakeStore | None = None) -> None:
        self.events: list[Event] = []
        self.releases = 0
        self.tasks = Tasks(TaskBoard(), self.publish)
        parts = HoldParts(
            self.tasks,
            lambda: ROLES,
            FakeTree(),
            self.publish,
            store,
            FakeHalt(),
            lambda role: None,
            lambda name, trigger: None,
        )
        self.holds = MergeHolds((STRESS,), 3, parts)
        self.holds.released = self.release

    async def publish(self, event: Event) -> None:
        self.events.append(event)

    def release(self) -> None:
        self.releases += 1

    def notices(self) -> list[str]:
        return [event.message for event in self.events if isinstance(event, Notice)]


async def held(store: FakeStore | None = None) -> Holds:
    """Holds whose holds kept were read, with :data:`CRASH` filed as task 1 in batch-2."""
    holds = Holds(store)
    await holds.holds.restore()
    await holds.holds.hold_finding(CRASH, "1", "batch-2")
    return holds


async def test_a_blocking_task_filed_holds_merges_with_its_notice_saved_and_shown() -> None:
    store = FakeStore()
    holds = await held(store)
    assert holds.holds.held and holds.holds.holds == (HELD,)
    assert holds.notices() == [note_finding_held(HELD)]
    assert store.holds == (HELD,)
    assert holds.holds.views == (
        MergeHold(
            set=BUGS_REVIEWER, commit="1a2b3c4d", reason="blocking finding", finding="Fix the crash"
        ),
    )
    assert holds.holds.checking == (HELD,)


async def test_the_fix_and_the_fixes_carrying_on_from_it_land_despite_the_holds() -> None:
    holds = await held()
    board = holds.tasks.board
    fix = board.add(TaskSpec(prompt="Fix the crash"))
    carried = board.add(TaskSpec(prompt="Fix the fix"), fixes=fix)
    other = board.add(TaskSpec(prompt="Something else"))
    assert holds.holds.is_fix(board.get(fix)) and holds.holds.is_fix(board.get(carried))
    assert not holds.holds.is_fix(board.get(other))
    assert holds.holds.fix_of(board.get(fix)) is None  # No set runs again as it lands.


async def test_a_blocking_task_filed_again_from_its_fixs_review_is_filed_as_its_next_fix() -> None:
    holds = await held()
    again = BlockingTask(BUGS_REVIEWER, "c9", "Fix the crash again", "batch-2")
    assert holds.holds.filing_of(again) == "fixed_again"
    assert holds.holds.filing_of(CRASH) == "held"
    await holds.holds.hold_finding(again, "4", "batch-5")
    [hold] = holds.holds.checking
    assert (hold.task, hold.batch, hold.fixes) == ("4", "batch-5", 2)
    assert holds.notices() == [note_finding_held(HELD)]


async def test_its_fixs_review_by_its_lens_lifts_the_hold_with_its_notice_and_releases() -> None:
    store = FakeStore()
    holds = await held(store)
    await holds.holds.hear_fix_review(
        FixReview("batch-2", frozenset({BUGS_REVIEWER, "tests-reviewer"}))
    )
    assert not holds.holds.held and store.holds == ()
    assert holds.notices()[-1] == finding_resumed_note(BUGS_REVIEWER)
    assert holds.releases == 2  # Once as no hold kept was read.


async def test_its_fixs_review_without_its_lens_leaves_the_hold_unchecked() -> None:
    holds = await held()
    await holds.holds.hear_fix_review(FixReview("batch-2", frozenset({"tests-reviewer"})))
    assert holds.holds.held and holds.holds.checking == ()
    assert holds.notices()[-1] == unchecked_note(BUGS_REVIEWER)
    assert holds.releases == 0


async def test_its_fixs_batch_closing_unreviewed_leaves_the_hold_unchecked() -> None:
    holds = await held()
    await holds.holds.hear_unreviewed(holds.holds.checking)
    assert holds.holds.held and holds.holds.checking == ()
    assert holds.notices()[-1] == unchecked_note(BUGS_REVIEWER)


async def test_merges_wait_for_a_sets_hold_and_a_findings_and_a_release_lifts_both() -> None:
    holds = await held()
    await holds.holds.heard(SetResult(STRESS, "c4", "nightly", (STEP, "2 failed")))
    await holds.holds.hear_fix_review(FixReview("batch-2", frozenset({BUGS_REVIEWER})))
    assert holds.holds.held and holds.releases == 0
    await holds.holds.hold_finding(CRASH, "7", "batch-3")
    await holds.holds.heard(SetResult(STRESS, "c5", "nightly", None))
    assert holds.holds.held and holds.releases == 0
    await holds.holds.release()
    assert not holds.holds.held and holds.releases == 1
    assert holds.notices()[-1] == (
        "Merges resumed by hand; the finding from bugs-reviewer isn't fixed."
    )


async def test_a_release_names_the_sets_not_passed_and_the_findings_not_fixed() -> None:
    holds = await held()
    await holds.holds.heard(SetResult(STRESS, "c4", "nightly", (STEP, "2 failed")))
    await holds.holds.release()
    assert holds.notices()[-1] == (
        "Merges resumed by hand; stress hasn't passed and the finding from bugs-reviewer isn't"
        " fixed."
    )
    assert [state.task.role for state in holds.tasks.board.tasks] == [CODER]


async def test_a_finding_hold_kept_is_restored_whichever_sets_hold_merges() -> None:
    store = FakeStore()
    store.holds = (HELD,)
    holds = Holds(store)
    await holds.holds.use_sets(())
    await holds.holds.restore()
    assert holds.holds.holds == (HELD,) and holds.releases == 0
    await holds.holds.use_sets((STRESS,))
    assert holds.holds.holds == (HELD,)


async def test_a_finding_hold_lifted_while_the_store_cant_be_read_stays_lifted_once_it_can() -> (
    None
):
    store = FakeStore()
    holds = await held(store)
    store.holds_load_failure = "disk gone"
    await holds.holds.restore()
    await holds.holds.hear_fix_review(FixReview("batch-2", frozenset({BUGS_REVIEWER})))
    store.holds_load_failure = None
    await holds.holds.restore()
    assert not holds.holds.held and store.holds == ()
