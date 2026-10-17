"""The task board as the application changes it: each change through it is published whole."""

from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.board import TaskBoard
from orbit.protocol import Event, TasksChanged, TaskSpec


def tasks_on(published: list[Event]) -> Tasks:
    """Tasks on a new board, publishing to ``published``."""

    async def publish(event: Event) -> None:
        published.append(event)

    return Tasks(TaskBoard(), publish)


async def test_publish_publishes_the_board_whole() -> None:
    published: list[Event] = []
    tasks = tasks_on(published)
    tasks.board.add(TaskSpec(prompt="a"))

    await tasks.publish()

    assert published == [TasksChanged(tasks=tasks.board.tasks)]


async def test_start_marks_the_task_running_and_publishes_it() -> None:
    published: list[Event] = []
    tasks = tasks_on(published)
    task_id = tasks.board.add(TaskSpec(prompt="a"))

    await tasks.start(task_id)

    assert tasks.board.get(task_id).status == "running"
    assert published == [TasksChanged(tasks=tasks.board.tasks)]


async def test_finish_ends_the_task_and_publishes_it() -> None:
    published: list[Event] = []
    tasks = tasks_on(published)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    await tasks.start(task_id)

    await tasks.finish(task_id, "completed")

    assert tasks.board.get(task_id).status == "completed"
    assert published[-1] == TasksChanged(tasks=tasks.board.tasks)
    assert len(published) == 2
