import pytest

from orbit.agent.domain.board import TaskBoard
from orbit.protocol import Settings, StepState, TaskSpec, TaskState

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
