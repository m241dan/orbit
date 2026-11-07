"""The board's lookup of tasks by id, kept in step with the run order through every change."""

import random
from collections.abc import Callable

import pytest

from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.refusals import Refused
from orbit.protocol import CONFIGURATOR, TaskSpec, TaskState

ONE = TaskSpec(prompt="one")
CONFIGURING = TaskSpec(prompt="set up", role=CONFIGURATOR)

Operation = Callable[[TaskBoard], object]


def assert_found_in_order(board: TaskBoard) -> None:
    """Assert each task on ``board`` is found, got and placed where run order has it, and that
    an id off it is found nowhere."""
    for index, state in enumerate(board.tasks):
        assert board.position(state.id) == index
        assert board.get(state.id) == state
        assert board.find(state.id) == state
    assert board.find("missing") is None
    assert board.find(None) is None
    with pytest.raises(Refused):
        board.position("missing")


def drafted(board: TaskBoard, count: int) -> list[str]:
    """The ids of ``count`` drafts the planner proposed, added to ``board``."""
    return [board.add_draft(ONE, "planner") for _ in range(count)]


def first_id(board: TaskBoard) -> str:
    return board.tasks[0].id


def last_id(board: TaskBoard) -> str:
    return board.tasks[-1].id


def move_last_draft_first(board: TaskBoard) -> None:
    """Add two drafts to ``board`` and move the second before the first."""
    first, second = drafted(board, 2)
    board.move_draft(second, first, "planner")


OPERATIONS: dict[str, Operation] = {
    "add": lambda board: board.add(ONE),
    "add a fix of the first": lambda board: board.add(ONE, fixes=first_id(board)),
    "add first": lambda board: board.add_first(ONE),
    "add a suggestion": lambda board: board.add_suggestion(ONE, "batch-1", 1),
    "add a suggestion first": lambda board: board.add_suggestion_first(ONE, "batch-1", 1),
    "add a draft": lambda board: board.add_draft(ONE, "planner"),
    "add a configurator's": lambda board: board.add_triggered(CONFIGURING, "setup"),
    "add a triggered task": lambda board: board.add_triggered(ONE, "nightly"),
    "append": lambda board: board.append(board.tasks[1]),
    "remove the first": lambda board: board.remove(first_id(board)),
    "remove the middle": lambda board: board.remove(board.tasks[2].id),
    "remove the last": lambda board: board.remove(last_id(board)),
    "remove a draft": lambda board: board.remove_draft(drafted(board, 1)[0], "planner"),
    "move a draft to the end": lambda board: board.move_draft(
        drafted(board, 2)[0], None, "planner"
    ),
    "move a draft before another": lambda board: move_last_draft_first(board),
    "replace by editing": lambda board: board.edit(board.tasks[1].id, TaskSpec(prompt="new")),
    "replace by accepting": lambda board: board.accept(drafted(board, 1)[0]),
}


def three_tasks() -> TaskBoard:
    board = TaskBoard()
    for _ in range(3):
        board.add(ONE)
    return board


@pytest.mark.parametrize("operation", OPERATIONS.values(), ids=OPERATIONS.keys())
def test_tasks_are_found_where_they_are_after_each_kind_of_change(operation: Operation) -> None:
    board = three_tasks()
    operation(board)
    assert_found_in_order(board)


def test_a_restored_board_finds_its_tasks_and_forgets_the_old_ones() -> None:
    board = three_tasks()
    restored = (TaskState(id="9", task=ONE), TaskState(id="7", task=ONE))
    board.restore(restored)
    assert_found_in_order(board)
    assert board.find("1") is None


def test_a_removed_tasks_fix_is_still_found_with_what_it_fixed_in_place() -> None:
    board = three_tasks()
    fix = board.add(ONE, fixes="2")
    board.remove("2")
    assert board.get(fix).fixes is None
    assert_found_in_order(board)


def add_fix(board: TaskBoard, model: list[str], choose: random.Random) -> None:
    """Add to ``board`` a fix of a task ``choose`` picks, and the same to ``model``."""
    fixed = choose.choice(model)
    model.insert(model.index(fixed) + 1, board.add(ONE, fixes=fixed))


def remove_one(board: TaskBoard, model: list[str], choose: random.Random) -> None:
    """Take a task ``choose`` picks off ``board`` and out of ``model``."""
    removed = choose.choice(model)
    board.remove(removed)
    model.remove(removed)


def change_at_random(board: TaskBoard, model: list[str], choose: random.Random) -> None:
    """Make a change to ``board`` chosen by ``choose``, and the same to ``model``, its ids in run
    order: adding at either end, adding a fix, or removing a task, the last two only once there
    are tasks."""
    match choose.randrange(4) if model else choose.randrange(2):
        case 0:
            model.append(board.add(ONE))
        case 1:
            model.insert(0, board.add_first(ONE))
        case 2:
            add_fix(board, model, choose)
        case _:
            remove_one(board, model, choose)


@pytest.mark.parametrize("seed", range(20))
def test_tasks_are_found_where_a_list_model_has_them_through_random_changes(seed: int) -> None:
    choose = random.Random(seed)
    board, model = TaskBoard(), list[str]()
    for _ in range(60):
        change_at_random(board, model, choose)
        assert [state.id for state in board.tasks] == model
        assert_found_in_order(board)
