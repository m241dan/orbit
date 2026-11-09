"""The task board as the application changes it: each change through it is published, whole or
as the one task it changed, and acted on, but for a turn's usage."""

import asyncio

import pytest

from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.refusals import Refused
from orbit.protocol import Event, TaskChanged, TasksChanged, TaskSpec, Usage


def tasks_on(published: list[Event], acted: list[str] | None = None) -> Tasks:
    """Tasks on a new board, publishing to ``published`` and noting in ``acted``, if given, each
    change acted on."""

    async def publish(event: Event) -> None:
        published.append(event)

    async def changed() -> None:
        if acted is not None:
            acted.append("changed")

    return Tasks(TaskBoard(), publish, changed)


USAGE = Usage(input_tokens=10)


async def test_publish_publishes_the_board_whole() -> None:
    published: list[Event] = []
    tasks = tasks_on(published)
    tasks.board.add(TaskSpec(prompt="a"))

    await tasks.publish()

    assert published == [TasksChanged(tasks=tasks.board.tasks)]


async def test_start_marks_the_task_running_and_publishes_that_task_alone() -> None:
    published: list[Event] = []
    tasks = tasks_on(published)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    tasks.board.add(TaskSpec(prompt="b"))

    await tasks.start(task_id)

    assert tasks.board.get(task_id).status == "running"
    assert published == [TaskChanged(task=tasks.board.get(task_id))]


async def test_finish_ends_the_task_and_publishes_it() -> None:
    published: list[Event] = []
    tasks = tasks_on(published)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    await tasks.start(task_id)

    await tasks.finish(task_id, "completed")

    assert tasks.board.get(task_id).status == "completed"
    assert published[-1] == TasksChanged(tasks=tasks.board.tasks)
    assert len(published) == 2


async def test_finish_records_why_the_task_ended() -> None:
    published: list[Event] = []
    tasks = tasks_on(published)
    task_id = tasks.board.add(TaskSpec(prompt="a"))

    await tasks.finish(task_id, "unverified", "Unit tests failed", "1 failed")

    state = tasks.board.get(task_id)
    assert (state.status, state.reason, state.reason_detail) == (
        "unverified",
        "Unit tests failed",
        "1 failed",
    )
    assert published == [TasksChanged(tasks=tasks.board.tasks)]


async def test_change_publishes_once_and_returns_the_changes_result() -> None:
    published: list[Event] = []
    tasks = tasks_on(published)

    task_id = await tasks.change(lambda board: board.add(TaskSpec(prompt="a")))

    assert tasks.board.get(task_id).task.prompt == "a"
    assert published == [TasksChanged(tasks=tasks.board.tasks)]


async def test_change_refused_by_the_board_publishes_nothing() -> None:
    published: list[Event] = []
    tasks = tasks_on(published)

    with pytest.raises(Refused):
        await tasks.change(lambda board: board.remove("missing"))

    assert published == []


def removes_missing(board: TaskBoard) -> str:
    """Removes a task that isn't on ``board``, which the board refuses."""
    board.remove("missing")
    return "removed"


def adds(board: TaskBoard) -> None:
    """Adds a task to ``board``."""
    board.add(TaskSpec(prompt="a"))


async def ignored(error: Refused) -> None:
    """Ignores the board refusing a change."""


async def test_change_refused_by_the_board_is_handed_to_refused_with_nothing_published() -> None:
    published: list[Event] = []
    tasks = tasks_on(published)
    errors: list[Refused] = []

    async def refused(error: Refused) -> str:
        errors.append(error)
        return "refused"

    assert await tasks.change(removes_missing, refused) == "refused"
    assert len(errors) == 1
    assert published == []


async def test_a_value_error_that_is_not_a_refusal_escapes_refused() -> None:
    published: list[Event] = []
    tasks = tasks_on(published)
    errors: list[Refused] = []

    async def refused(error: Refused) -> None:
        errors.append(error)

    def breaks(board: TaskBoard) -> None:
        raise ValueError("a bug in the change")

    with pytest.raises(ValueError, match="a bug in the change"):
        await tasks.change(breaks, refused)

    assert errors == []
    assert published == []


async def test_a_value_error_after_the_board_changed_escapes_refused() -> None:
    async def publish(event: Event) -> None:
        raise ValueError("publishing tripped")

    tasks = Tasks(TaskBoard(), publish)

    with pytest.raises(ValueError, match="publishing tripped"):
        await tasks.change(adds, ignored)

    assert len(tasks.board.tasks) == 1


async def test_a_change_made_elsewhere_is_acted_on_with_nothing_published() -> None:
    published: list[Event] = []
    acted: list[str] = []

    async def changed() -> None:
        acted.append("changed")

    async def publish(event: Event) -> None:
        published.append(event)

    await Tasks(TaskBoard(), publish, changed).changed_elsewhere()

    assert (acted, published) == (["changed"], [])


async def test_a_wait_for_a_change_ends_once_the_board_is_published() -> None:
    tasks = tasks_on([])
    waits = asyncio.create_task(tasks.moved())
    await asyncio.sleep(0)
    assert not waits.done()

    await tasks.change(adds)

    await asyncio.wait_for(waits, 1)


async def test_a_wait_begun_after_a_change_waits_for_the_next() -> None:
    tasks = tasks_on([])
    await tasks.change(adds)

    waits = asyncio.create_task(tasks.moved())
    await asyncio.sleep(0)

    assert not waits.done()
    waits.cancel()


async def test_a_change_to_one_task_publishes_that_task_alone_and_is_acted_on() -> None:
    published: list[Event] = []
    acted: list[str] = []
    tasks = tasks_on(published, acted)
    tasks.board.add(TaskSpec(prompt="a"))
    task_id = tasks.board.add(TaskSpec(prompt="b"))

    await tasks.change_task(task_id, lambda board: board.set_prompt(task_id, "b!"))

    assert published == [TaskChanged(task=tasks.board.get(task_id))]
    assert tasks.board.get(task_id).task.prompt == "b!"
    assert acted == ["changed"]


async def test_a_change_to_one_task_the_board_refuses_is_handed_to_refused_unpublished() -> None:
    published: list[Event] = []
    acted: list[str] = []
    tasks = tasks_on(published, acted)

    async def refused(error: Refused) -> str:
        return error.message

    def edits(board: TaskBoard) -> str:
        board.set_status("missing", "running")
        return "edited"

    assert await tasks.change_task("missing", edits, refused) == "No task with id 'missing'."
    assert (published, acted) == ([], [])


async def test_a_turns_usage_publishes_its_task_alone_and_is_not_acted_on() -> None:
    published: list[Event] = []
    acted: list[str] = []
    tasks = tasks_on(published, acted)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    tasks.board.add(TaskSpec(prompt="b"))

    await tasks.add_usage(task_id, USAGE, ignored)

    assert tasks.board.get(task_id).usage == USAGE
    assert published == [TaskChanged(task=tasks.board.get(task_id))]
    assert acted == []


async def test_a_change_of_status_after_a_turns_usage_is_acted_on() -> None:
    acted: list[str] = []
    tasks = tasks_on([], acted)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    await tasks.add_usage(task_id, USAGE, ignored)

    await tasks.start(task_id)

    assert acted == ["changed"]


async def test_a_removed_tasks_usage_is_handed_to_refused_unpublished() -> None:
    published: list[Event] = []
    tasks = tasks_on(published)
    errors: list[Refused] = []

    async def refused(error: Refused) -> None:
        errors.append(error)

    await tasks.add_usage("missing", USAGE, refused)

    assert [error.message for error in errors] == ["No task with id 'missing'."]
    assert published == []


async def test_a_wait_for_a_change_ends_once_a_task_of_the_board_is_published() -> None:
    tasks = tasks_on([])
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    waits = asyncio.create_task(tasks.moved())
    await asyncio.sleep(0)

    await tasks.add_usage(task_id, USAGE, ignored)

    await asyncio.wait_for(waits, 1)
