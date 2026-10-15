import pytest

from orbit.agent.domain.board import TaskBoard
from orbit.protocol import Settings, StepState, TaskSpec, TaskState, Usage

ONE = TaskSpec(prompt="one")
TWO = TaskSpec(prompt="two", settings=Settings(model="opus"))
FIX = TaskSpec(prompt="fix it")


def prompts(board: TaskBoard) -> list[str]:
    return [t.task.prompt for t in board.tasks]


def test_starts_empty_with_pending_steps() -> None:
    board = TaskBoard(("Unit tests", "Lint"))
    assert board.tasks == ()
    assert board.steps == (StepState(name="Unit tests"), StepState(name="Lint"))
    assert TaskBoard().steps == ()


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
    with pytest.raises(ValueError, match="No task with id 'nope'"):
        board.edit("nope", TWO)
    for status in ("running", "verifying"):
        board.set_status(first, status)
        with pytest.raises(ValueError, match="can't be edited while it runs"):
            board.edit(first, TWO)
    assert board.get(first).task == ONE


def test_next_runnable_skips_done_running_and_already_tried_tasks() -> None:
    board = TaskBoard()
    ids = [board.add(TaskSpec(prompt=str(i))) for i in range(7)]
    statuses = [
        "completed",
        "unverified",
        "running",
        "verifying",
        "error",
        "cancelled",
        "max_steps",
    ]
    for task_id, status in zip(ids, statuses, strict=True):
        board.set_status(task_id, status)  # type: ignore[arg-type]
    # Tasks that ended badly are worth another run; finished or running ones aren't.
    assert board.next_runnable() == ids[4]
    assert board.next_runnable(skip={ids[4]}) == ids[5]
    assert board.next_runnable(skip=set(ids[4:])) is None


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


def test_step_statuses_follow_a_verification_and_reset() -> None:
    board = TaskBoard(("Unit tests", "Lint"))
    board.set_step(0, "passed")
    board.set_step(1, "running")
    board.cancel_running_steps()
    assert [s.status for s in board.steps] == ["passed", "cancelled"]
    board.reset_steps()
    assert [s.status for s in board.steps] == ["pending", "pending"]
    board.skip_steps()
    assert [s.status for s in board.steps] == ["skipped", "skipped"]


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
    assert board.get(review).origin == "review"


def test_tasks_given_up_on_are_not_run_again() -> None:
    board = TaskBoard()
    task_id = board.add(ONE)
    board.set_status(task_id, "gave_up")
    assert board.next_runnable() is None


def test_fixes_inherit_the_origin_batch_and_round_of_what_they_fix() -> None:
    board = TaskBoard()
    suggestion = board.add_suggestion(TaskSpec(prompt="tidy"), batch="batch-1", round=2)
    assert board.get(suggestion).origin == "review"
    fix = board.add(FIX, fixes=suggestion)
    state = board.get(fix)
    assert (state.origin, state.batch, state.round) == ("review", "batch-1", 2)


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
    assert (board.get(draft).status, board.get(draft).origin) == ("draft", "planner")
    assert board.next_runnable() is None
    board.accept(draft)
    assert board.get(draft).status == "pending"
    assert board.next_runnable() == draft


def test_only_drafts_can_be_accepted() -> None:
    board = TaskBoard()
    task_id = board.add(ONE)
    with pytest.raises(ValueError, match="isn't a draft"):
        board.accept(task_id)


def test_tasks_can_be_removed_unless_they_run() -> None:
    board = TaskBoard()
    first, second = board.add(ONE), board.add(TWO)
    board.remove(first)
    assert [t.id for t in board.tasks] == [second]
    board.set_status(second, "running")
    with pytest.raises(ValueError, match="while it runs"):
        board.remove(second)


def test_steps_can_be_replaced_none_of_them_run() -> None:
    board = TaskBoard(["Old"])
    board.set_step(0, "passed")
    board.set_steps(["Lint", "Tests"])
    assert board.steps == (StepState(name="Lint"), StepState(name="Tests"))


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
