"""How a lane's task run ends when it fails: git, the host and saving the config each with an
error of their own, and anything else as a bug."""

import pytest

from orbit.agent.application.outcomes import Outcome, UserStop, run_task_to_end
from orbit.agent.application.ports import (
    ConfigWriteFailed,
    HostFailed,
    StorageFailed,
    TreeFailed,
)
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.endings import BUG
from orbit.agent.domain.storage_halt import StorageErrorKind
from orbit.protocol import AGENT_FAILED, GIT_FAILED, ErrorOccurred, Event, TaskSpec

STOP = UserStop("Stopped by you")


class Run:
    """A task's run that fails with ``failure``, and the events its lane published."""

    def __init__(self, failure: Exception) -> None:
        self.failure = failure
        self.events: list[Event] = []

    async def __call__(self) -> Outcome:
        raise self.failure

    async def publish(self, event: Event) -> None:
        self.events.append(event)


class UnstartableTasks(Tasks):
    """Tasks whose start fails as the store's write does."""

    async def start(self, task_id: str) -> None:
        raise StorageFailed("The board couldn't be saved.", "orbit.db", "disk I/O error", KIND)


KIND = StorageErrorKind.UNKNOWN


def board_with_task(tasks_type: type[Tasks], run: Run) -> tuple[Tasks, str]:
    """``tasks_type`` on a new board publishing to ``run``, and a pending task's id on it."""
    tasks = tasks_type(TaskBoard(), run.publish)
    return tasks, tasks.board.add(TaskSpec(prompt="a"))


def ending(tasks: Tasks, task_id: str) -> tuple[str, str, str]:
    state = tasks.board.get(task_id)
    return state.status, state.reason, state.reason_detail


async def ended_by(failure: Exception) -> tuple[Run, tuple[str, str, str]]:
    """The run failing with ``failure``, once its task has run to its end, and that end."""
    run = Run(failure)
    tasks, task_id = board_with_task(Tasks, run)
    await run_task_to_end(tasks, task_id, run, STOP, run.publish)
    return run, ending(tasks, task_id)


async def test_a_tree_failure_is_published_as_git_failed_and_ends_the_task_in_error() -> None:
    run, end = await ended_by(TreeFailed("git worktree add failed"))
    assert ErrorOccurred(code=GIT_FAILED, message="git worktree add failed") in run.events
    assert end == ("error", "Error: git worktree add failed", "git worktree add failed")


async def test_a_host_failure_is_published_as_agent_failed_and_ends_the_task_in_error() -> None:
    message = "Orbit's MCP server failed to start."
    run, end = await ended_by(HostFailed(message))
    assert ErrorOccurred(code=AGENT_FAILED, message=message) in run.events
    assert end == ("error", f"Error: {message}", message)


async def test_a_config_write_failure_is_published_as_agent_failed_and_ends_in_error() -> None:
    message = "Couldn't save the checks to .orbit/config.toml: read-only"
    run, end = await ended_by(ConfigWriteFailed(message))
    assert ErrorOccurred(code=AGENT_FAILED, message=message) in run.events
    assert end == ("error", f"Error: {message}", message)


async def test_a_plain_runtime_error_ends_the_task_as_a_bug_and_escapes() -> None:
    run = Run(RuntimeError("not git"))
    tasks, task_id = board_with_task(Tasks, run)
    with pytest.raises(RuntimeError, match="not git"):
        await run_task_to_end(tasks, task_id, run, STOP, run.publish)
    assert ending(tasks, task_id) == ("error", BUG, "not git")
    assert not [e for e in run.events if isinstance(e, ErrorOccurred)]


async def test_a_storage_failure_starting_the_task_is_not_reported_as_git_failed() -> None:
    run = Run(AssertionError("never run"))
    tasks, task_id = board_with_task(UnstartableTasks, run)
    with pytest.raises(StorageFailed):
        await run_task_to_end(tasks, task_id, run, STOP, run.publish)
    assert not [e for e in run.events if isinstance(e, ErrorOccurred)]
    assert ending(tasks, task_id) == ("pending", "", "")
