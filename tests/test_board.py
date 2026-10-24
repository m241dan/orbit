import pytest

from orbit.agent.domain.board import SETTLED, WAITING, TaskBoard, interrupted
from orbit.agent.domain.endings import INTERRUPTED
from orbit.agent.domain.refusals import Refused
from orbit.protocol import (
    ACTIVE_STATUSES,
    BUSY,
    INVALID,
    PLANNER_ORIGIN,
    REVIEW_ORIGIN,
    SUPER_REVIEW_ORIGIN,
    UNKNOWN,
    Settings,
    TaskSpec,
    TaskState,
    TaskStatus,
    Usage,
)

ONE = TaskSpec(prompt="one")
TWO = TaskSpec(prompt="two", settings=Settings(model="opus"))
FIX = TaskSpec(prompt="fix it")


def prompts(board: TaskBoard) -> list[str]:
    return [t.task.prompt for t in board.tasks]


def test_active_tasks_are_waiting_and_settled() -> None:
    assert ACTIVE_STATUSES <= WAITING
    assert ACTIVE_STATUSES <= SETTLED


def test_starts_empty() -> None:
    assert TaskBoard().tasks == ()


def test_add_appends_pending_tasks_with_distinct_ids() -> None:
    board = TaskBoard()
    first, second = board.add(ONE), board.add(TWO)
    assert first != second
    assert board.tasks == (TaskState(id=first, task=ONE), TaskState(id=second, task=TWO))
    assert board.get(second).task == TWO
    assert board.position(second) == 1


def test_a_fix_goes_right_behind_the_task_it_fixes() -> None:
    board = TaskBoard()
    first = board.add(ONE)
    board.add(TWO)
    fix = board.add(FIX, fixes=first)
    assert prompts(board) == ["one", "fix it", "two"]
    assert board.get(fix).fixes == first
    assert board.position(fix) == 1


def test_edit_replaces_the_task_and_resets_it_to_pending() -> None:
    board = TaskBoard()
    first = board.add(ONE)
    board.set_status(first, "completed")
    board.edit(first, TWO)
    assert board.tasks == (TaskState(id=first, task=TWO),)


def test_edit_rejects_unknown_and_running_tasks() -> None:
    board = TaskBoard()
    first = board.add(ONE)
    with pytest.raises(Refused, match="No task with id 'nope'") as unknown:
        board.edit("nope", TWO)
    assert unknown.value.code == UNKNOWN
    for status in ("running", "verifying"):
        board.set_status(first, status)
        with pytest.raises(Refused, match="can't be edited while it runs") as running:
            board.edit(first, TWO)
        assert running.value.code == BUSY
    assert board.get(first).task == ONE


def test_edit_refuses_a_review_orbit_queued_but_not_one_the_user_added() -> None:
    board = TaskBoard()
    review = TaskSpec(prompt="Review commit abc", role="reviewer")
    queued, added = board.add_review(review), board.add(review)
    board.set_status(queued, "completed")
    with pytest.raises(Refused, match="Reviews can't be edited") as refused:
        board.edit(queued, TWO)
    assert refused.value.code == INVALID
    board.edit(added, TWO)
    assert [t.task for t in board.tasks] == [review, TWO]


def test_next_runnable_skips_done_running_and_already_tried_tasks() -> None:
    board = TaskBoard()
    ids = [board.add(TaskSpec(prompt=str(i))) for i in range(6)]
    statuses = [
        "completed",
        "unverified",
        "running",
        "verifying",
        "error",
        "cancelled",
    ]
    for task_id, status in zip(ids, statuses, strict=True):
        board.set_status(task_id, status)  # type: ignore[arg-type]
    # Tasks that ended badly are worth another run; finished or running ones aren't.
    assert board.next_runnable() == ids[4]
    assert board.next_runnable(skip={ids[4]}) == ids[5]
    assert board.next_runnable(skip=set(ids[4:])) is None


def test_next_runnable_takes_first_before_board_order_while_it_is_runnable() -> None:
    board = TaskBoard()
    ids = [board.add(TaskSpec(prompt=str(i))) for i in range(3)]
    board.set_status(ids[0], "error")
    board.set_status(ids[2], "error")
    assert board.next_runnable(first=ids[2]) == ids[2]
    assert board.next_runnable(skip={ids[2]}, first=ids[2]) == ids[0]
    board.set_status(ids[2], "completed")
    assert board.next_runnable(first=ids[2]) == ids[0]
    assert board.next_runnable(first="gone") == ids[0]


def fix_after_an_error(fix_status: TaskStatus) -> tuple[TaskBoard, str, str]:
    """A board whose first task ended in error, and whose second, unverified, has a fix that
    ended in ``fix_status``; returns it, the first task's id and the fix's."""
    board = TaskBoard()
    first = board.add(ONE)
    second = board.add(TWO)
    board.finish(first, "error", "Crashed")
    board.finish(second, "unverified", "Lint failed")
    fix = board.add(FIX, fixes=second)
    board.finish(fix, fix_status, "Crashed")
    return board, first, fix


@pytest.mark.parametrize("status", ["error", "cancelled"])
def test_next_runnable_takes_a_fix_cut_short_before_earlier_tasks(status: TaskStatus) -> None:
    board, first, fix = fix_after_an_error(status)
    assert board.next_runnable() == fix
    assert board.next_runnable(first=first) == fix
    assert board.next_runnable(skip={fix}) == first


@pytest.mark.parametrize("status", ["pending", "unverified", "gave_up"])
def test_next_runnable_keeps_board_order_for_a_fix_still_to_run_or_settled(
    status: TaskStatus,
) -> None:
    board, first, _ = fix_after_an_error(status)
    assert board.next_runnable() == first


def test_next_runnable_limits_a_fix_cut_short_to_roles_and_excludes() -> None:
    board, _, fix = fix_after_an_error("error")
    assert board.next_runnable(roles={"coder"}) == fix
    assert board.next_runnable(roles={"reviewer"}) is None
    assert board.next_runnable(exclude={"coder"}) is None
    board.add_first(TaskSpec(prompt="review", role="reviewer"))
    assert board.next_runnable(roles={"reviewer"}) == board.tasks[0].id


def test_a_passing_fix_completes_the_tasks_it_fixed() -> None:
    board = TaskBoard()
    first = board.add(ONE)
    other = board.add(TWO)
    board.set_status(first, "unverified")
    board.set_status(other, "unverified")
    # The first fix failed too, so a second one fixes it.
    fix = board.add(FIX, fixes=first)
    board.set_status(fix, "unverified")
    fix_of_fix = board.add(FIX, fixes=fix)

    board.set_status(fix_of_fix, "completed")
    board.mark_fixed(fix_of_fix)
    assert [t.status for t in board.tasks] == ["completed", "completed", "completed", "unverified"]


def test_marking_fixed_leaves_tasks_that_changed_since_alone() -> None:
    board = TaskBoard()
    first = board.add(ONE)
    board.set_status(first, "unverified")
    fix = board.add(FIX, fixes=first)
    board.edit(first, TWO)  # pending again, to be run as edited
    board.edit(fix, FIX)  # still the fix for that task
    assert board.get(fix).fixes == first

    board.set_status(fix, "completed")
    board.mark_fixed(fix)
    assert [t.status for t in board.tasks] == ["pending", "completed"]
    # A task nobody queued a fix for has nothing to mark.
    board.mark_fixed(first)
    assert board.get(first).status == "pending"


def test_finishing_a_fix_completes_what_it_fixed_but_other_endings_do_not() -> None:
    board = TaskBoard()
    first = board.add(ONE)
    board.finish(first, "unverified")
    fix = board.add(FIX, fixes=first)
    board.finish(fix, "error")
    assert [t.status for t in board.tasks] == ["unverified", "error"]
    board.finish(fix, "completed")
    assert [t.status for t in board.tasks] == ["completed", "completed"]


def reasons(board: TaskBoard) -> list[tuple[str, str]]:
    return [(t.reason, t.reason_detail) for t in board.tasks]


def test_finishing_with_a_reason_records_it() -> None:
    board = TaskBoard()
    first = board.add(ONE)
    board.finish(first, "unverified", "Unit tests failed", "2 failed, 5 passed")
    assert board.get(first).status == "unverified"
    assert reasons(board) == [("Unit tests failed", "2 failed, 5 passed")]


def test_finishing_as_completed_leaves_no_reason() -> None:
    board = TaskBoard()
    first = board.add(ONE)
    board.finish(first, "error", "Crashed", "Traceback")
    board.finish(first, "completed")
    assert reasons(board) == [("", "")]


def test_running_again_clears_the_reason() -> None:
    board = TaskBoard()
    first = board.add(ONE)
    board.finish(first, "error", "Crashed", "Traceback")
    board.set_status(first, "running")
    assert reasons(board) == [("", "")]


def test_other_status_changes_keep_the_reason() -> None:
    board = TaskBoard()
    first = board.add(ONE)
    board.finish(first, "error", "Crashed", "Traceback")
    board.set_status(first, "cancelled")
    assert reasons(board) == [("Crashed", "Traceback")]


def test_editing_clears_the_reason() -> None:
    board = TaskBoard()
    first = board.add(ONE)
    board.finish(first, "gave_up", "Gave up", "It contradicts itself")
    board.edit(first, TWO)
    assert reasons(board) == [("", "")]


def test_a_passing_fix_clears_the_reasons_of_the_tasks_it_completes() -> None:
    board = TaskBoard()
    first = board.add(ONE)
    board.finish(first, "unverified", "Unit tests failed", "1 failed")
    fix = board.add(FIX, fixes=first)
    board.finish(fix, "unverified", "Lint failed", "E501")
    fix_of_fix = board.add(FIX, fixes=fix)
    board.finish(fix_of_fix, "completed")
    assert [t.status for t in board.tasks] == ["completed"] * 3
    assert reasons(board) == [("", "")] * 3


def test_a_failing_fix_leaves_the_reason_of_what_it_fixed() -> None:
    board = TaskBoard()
    first = board.add(ONE)
    board.finish(first, "unverified", "Unit tests failed", "1 failed")
    fix = board.add(FIX, fixes=first)
    board.finish(fix, "error", "Crashed", "Traceback")
    assert reasons(board) == [("Unit tests failed", "1 failed"), ("Crashed", "Traceback")]


def test_next_runnable_can_be_limited_to_roles_or_exclude_them() -> None:
    board = TaskBoard()
    review = board.add(TaskSpec(prompt="review", role="reviewer"))
    code = board.add(TaskSpec(prompt="code"))
    assert board.next_runnable(roles={"coder"}) == code
    assert board.next_runnable(roles={"planner"}) is None
    assert board.next_runnable(exclude={"reviewer"}) == code
    assert board.next_runnable(roles={"reviewer", "super-reviewer"}) == review


def test_reviews_orbit_queues_are_marked_as_from_review() -> None:
    board = TaskBoard()
    review = board.add_review(TaskSpec(prompt="Review commit abc", role="reviewer"))
    assert board.get(review).origin == REVIEW_ORIGIN


def test_tasks_given_up_on_are_not_run_again() -> None:
    board = TaskBoard()
    task_id = board.add(ONE)
    board.set_status(task_id, "gave_up")
    assert board.next_runnable() is None


def test_fixes_inherit_the_origin_batch_and_round_of_what_they_fix() -> None:
    board = TaskBoard()
    suggestion = board.add_suggestion(TaskSpec(prompt="tidy"), batch="batch-1", round=2)
    assert board.get(suggestion).origin == REVIEW_ORIGIN
    fix = board.add(FIX, fixes=suggestion)
    state = board.get(fix)
    assert (state.origin, state.batch, state.round) == ("review", "batch-1", 2)


def test_suggestions_take_the_origin_they_are_given() -> None:
    board = TaskBoard()
    suggestion = board.add_suggestion(
        TaskSpec(prompt="tidy"), batch="batch-1", round=0, origin=SUPER_REVIEW_ORIGIN
    )
    assert board.get(suggestion).origin == SUPER_REVIEW_ORIGIN


def test_suggestions_join_the_end_of_the_queue() -> None:
    board = TaskBoard()
    first = board.add(ONE)
    suggestion = board.add_suggestion(TaskSpec(prompt="tidy"), batch="batch-1", round=1)
    assert [t.id for t in board.tasks] == [first, suggestion]


def test_editing_a_suggestion_keeps_where_it_came_from() -> None:
    board = TaskBoard()
    suggestion = board.add_suggestion(TaskSpec(prompt="tidy"), batch="batch-1", round=1)
    board.set_status(suggestion, "error")
    board.set_attempt(suggestion, 2, 5)
    board.edit(suggestion, TaskSpec(prompt="tidy more"))
    state = board.get(suggestion)
    assert (state.origin, state.batch, state.round) == ("review", "batch-1", 1)
    assert (state.status, state.attempt, state.task.prompt) == ("pending", 0, "tidy more")


def test_drafts_wait_until_accepted() -> None:
    board = TaskBoard()
    draft = board.add_draft(TaskSpec(prompt="plan step"))
    assert (board.get(draft).status, board.get(draft).origin) == ("draft", PLANNER_ORIGIN)
    assert board.next_runnable() is None
    board.accept(draft)
    assert board.get(draft).status == "pending"
    assert board.next_runnable() == draft


def test_only_drafts_can_be_accepted() -> None:
    board = TaskBoard()
    task_id = board.add(ONE)
    with pytest.raises(Refused, match="isn't a draft") as refused:
        board.accept(task_id)
    assert refused.value.code == INVALID


def test_tasks_can_be_removed_unless_they_run() -> None:
    board = TaskBoard()
    first, second = board.add(ONE), board.add(TWO)
    board.remove(first)
    assert [t.id for t in board.tasks] == [second]
    board.set_status(second, "running")
    with pytest.raises(ValueError, match="while it runs") as refused:
        board.remove(second)
    assert isinstance(refused.value, Refused)
    assert refused.value.code == BUSY
    assert str(refused.value) == refused.value.message == "Task '2' can't be removed while it runs."


def test_removing_a_task_makes_its_fixes_top_level_where_they_were() -> None:
    board = TaskBoard()
    suggestion = board.add_suggestion(TaskSpec(prompt="tidy"), batch="batch-1", round=2)
    fix = board.add(FIX, fixes=suggestion)
    board.remove(suggestion)
    state = board.get(fix)
    assert state.fixes is None
    assert (state.origin, state.batch, state.round) == ("review", "batch-1", 2)


def test_removing_the_middle_of_a_chain_keeps_the_chain() -> None:
    board = TaskBoard()
    first = board.add(ONE)
    board.finish(first, "unverified")
    fix = board.add(FIX, fixes=first)
    board.finish(fix, "unverified")
    fix_of_fix = board.add(FIX, fixes=fix)
    board.remove(fix)
    assert board.get(fix_of_fix).fixes == first
    board.finish(fix_of_fix, "completed")
    assert [t.status for t in board.tasks] == ["completed", "completed"]


def test_a_fix_whose_fixed_task_was_removed_still_completes() -> None:
    board = TaskBoard()
    first = board.add(ONE)
    board.finish(first, "unverified")
    fix = board.add(FIX, fixes=first)
    board.remove(first)
    board.finish(fix, "completed")
    assert [(t.id, t.status) for t in board.tasks] == [(fix, "completed")]


def test_each_task_adds_up_the_usage_of_its_own_turns() -> None:
    board = TaskBoard()
    first, second = board.add(ONE), board.add(TWO)
    board.add_usage(first, Usage(input_tokens=100, cost_usd=0.5, turns=1))
    board.add_usage(first, Usage(output_tokens=20, cost_usd=0.25, turns=2))
    board.add_usage(second, Usage(input_tokens=7, cost_usd=0.01))
    assert board.get(first).usage == Usage(
        input_tokens=100, output_tokens=20, cost_usd=0.75, turns=3
    )
    assert board.get(second).usage == Usage(input_tokens=7, cost_usd=0.01)
    # Editing a task makes it pending again, but what it spent stays spent.
    board.edit(first, TWO)
    assert board.get(first).usage.tokens == 120


def test_restore_keeps_the_given_tasks_in_order() -> None:
    saved = (TaskState(id="3", task=TWO), TaskState(id="1", task=ONE, status="completed"))
    board = TaskBoard()
    board.restore(saved)
    assert board.tasks == saved


def test_restore_replaces_what_the_board_held() -> None:
    board = TaskBoard()
    board.add(ONE)
    board.add(TWO)
    board.restore((TaskState(id="7", task=FIX),))
    assert prompts(board) == ["fix it"]


def test_restored_ids_carry_on_after_the_highest() -> None:
    board = TaskBoard()
    board.restore((TaskState(id="5", task=ONE), TaskState(id="2", task=TWO)))
    assert board.add(FIX) == "6"


def test_restored_ids_skip_past_tasks_removed_before_saving() -> None:
    board = TaskBoard()
    ids = [board.add(TaskSpec(prompt=str(i))) for i in range(3)]
    board.remove(ids[-1])
    saved = board.tasks
    restored = TaskBoard()
    restored.restore(saved)
    assert restored.add(FIX) == "3"


def test_restoring_no_tasks_starts_ids_again_from_one() -> None:
    board = TaskBoard()
    board.add(ONE)
    board.restore(())
    assert board.tasks == ()
    assert board.add(TWO) == "1"


def test_restore_keeps_non_numeric_ids_but_ignores_them_for_the_next_id() -> None:
    board = TaskBoard()
    saved = (TaskState(id="abc", task=ONE), TaskState(id="2", task=TWO))
    board.restore(saved)
    assert board.tasks == saved
    assert board.add(FIX) == "3"


@pytest.mark.parametrize("status", ["running", "verifying"])
def test_a_crash_cancels_tasks_that_were_running(status: TaskStatus) -> None:
    task = TaskState(id="1", task=ONE, status=status)
    cancelled = task.model_copy(update={"status": "cancelled", "reason": INTERRUPTED})
    assert interrupted((task,)) == ((cancelled,), 1)


def test_a_crash_gives_its_reason_in_place_of_any_earlier_one() -> None:
    task = TaskState(id="1", task=ONE, status="running", reason="Old", reason_detail="Old detail")
    (left,), _ = interrupted((task,))
    assert (left.reason, left.reason_detail) == (INTERRUPTED, "")


@pytest.mark.parametrize(
    "status",
    ["draft", "pending", "completed", "unverified", "gave_up", "cancelled", "error"],
)
def test_a_crash_leaves_tasks_that_were_not_running_alone(status: TaskStatus) -> None:
    task = TaskState(id="1", task=ONE, status=status, reason="Lint failed", reason_detail="E501")
    assert interrupted((task,)) == ((task,), 0)


def test_a_crash_keeps_everything_but_the_status_and_reason_and_counts_each_change() -> None:
    usage = Usage(input_tokens=5)
    fixed = TaskState(id="1", task=ONE, status="unverified", origin="review", batch="b", round=2)
    fix = TaskState(
        id="2", task=FIX, status="running", fixes="1", attempt=1, max_attempts=3, usage=usage
    )
    other = TaskState(id="3", task=TWO, status="verifying")
    tasks, changed = interrupted((fixed, fix, other))
    cancelled = {"status": "cancelled", "reason": INTERRUPTED}
    assert tasks == (
        fixed,
        fix.model_copy(update=cancelled),
        other.model_copy(update=cancelled),
    )
    assert changed == 2


def test_an_appended_task_keeps_its_state_under_the_next_id() -> None:
    board = TaskBoard()
    board.restore((TaskState(id="4", task=ONE),))
    draft = TaskState(id="1", task=FIX, status="draft", origin="planner")
    assert board.append(draft) == "5"
    assert board.tasks[-1] == draft.model_copy(update={"id": "5"})


@pytest.mark.parametrize("status", ["pending", "running"])
def test_a_pending_or_running_task_in_a_role_is_waiting(status: TaskStatus) -> None:
    board = TaskBoard()
    task_id = board.add(TaskSpec(prompt="configure", role="configurator"))
    board.set_status(task_id, status)
    assert board.has_waiting("configurator")


def test_a_finished_task_or_a_draft_is_not_waiting() -> None:
    board = TaskBoard()
    board.finish(board.add(TaskSpec(prompt="done", role="configurator")), "completed")
    board.add_draft(TaskSpec(prompt="draft", role="configurator"))
    assert not board.has_waiting("configurator")


def test_a_waiting_task_in_another_role_does_not_count() -> None:
    board = TaskBoard()
    board.add(ONE)
    assert not board.has_waiting("configurator")
