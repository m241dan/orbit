"""How a lane's task ends: run from its start under a guard that always records its end, so a
task is never left running, however its run is cancelled or breaks."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from orbit.agent.application.ports import (
    ConfigWriteFailed,
    HostFailed,
    Publish,
    TempDirFailed,
    TreeFailed,
)
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.verifier import Failure
from orbit.agent.domain.checks import ChecksProposal
from orbit.agent.domain.endings import CANCELLED, Ending, bug, error
from orbit.protocol import ACTIVE_STATUSES, AGENT_FAILED, GIT_FAILED, ErrorOccurred, TaskStatus


def cancelling() -> bool:
    """Whether the task running now is being cancelled."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


@dataclass(frozen=True)
class Outcome:
    """How a task ended: its status, the failure to hand a fresh agent if it fell back, the
    agent's summaries of what it did, one per claim, the checks the user accepted from it, and
    why it ended badly, in a line (``reason``) and in full (``detail``)."""

    status: TaskStatus
    failure: Failure | None = None
    summaries: tuple[str, ...] = ()
    checks: ChecksProposal | None = None
    reason: str = ""
    detail: str = ""


def ended(status: TaskStatus, why: Ending) -> Outcome:
    """A task's ending as ``status``, with nothing to show for it, for the reason ``why``."""
    reason, detail = why
    return Outcome(status, reason=reason, detail=detail)


async def see_to_end(
    tasks: Tasks, task_id: str, default: Outcome, outcome: Awaitable[Outcome]
) -> Outcome:
    """The ``outcome`` task ``task_id`` of ``tasks`` reached, recorded as its end once it's
    started. It ends as ``default`` if ``outcome`` is cancelled, and in error, re-raised, if
    ``outcome`` breaks on a bug: a task left running could never be run or dropped. So
    ``outcome`` is what starts the task, leaving no await between its start and this guard; a
    task it never started is left as it was."""
    reached = default
    try:
        reached = await outcome
    except Exception as exc:
        reached = ended("error", bug(exc))
        raise
    finally:
        if tasks.board.get(task_id).status in ACTIVE_STATUSES:
            await tasks.finish(task_id, reached.status, reached.reason, reached.detail)
    return reached


async def tree_failed(publish: Publish, exc: TreeFailed) -> Outcome:
    """A task's ending in error because git or its tree failed with ``exc``, published with
    ``publish``."""
    return await _failed(publish, GIT_FAILED, exc)


async def run_failed(
    publish: Publish, exc: HostFailed | TempDirFailed | ConfigWriteFailed
) -> Outcome:
    """A task's ending in error because its run failed with ``exc``, the host agents reach orbit
    through not serving, its temporary directory not being made or the checks it proposed not
    saving, published with ``publish``."""
    return await _failed(publish, AGENT_FAILED, exc)


async def _failed(publish: Publish, code: str, exc: Exception) -> Outcome:
    """A task's ending in error because of ``exc``, published with ``publish`` as ``code``."""
    await publish(ErrorOccurred(code=code, message=str(exc)))
    return ended("error", error(str(exc)))


async def _nothing() -> None:
    """Clean up nothing."""


@dataclass(frozen=True)
class UserStop:
    """How a lane's task the user stops ends: cancelled for ``reason``, once ``cleanup`` has
    run. ``cleanup`` runs however the task is cancelled."""

    reason: str
    cleanup: Callable[[], Awaitable[None]] = _nothing


async def run_task_to_end(
    tasks: Tasks,
    task_id: str,
    run: Callable[[], Awaitable[Outcome]],
    stop: UserStop,
    publish: Publish,
) -> Outcome:
    """How task ``task_id`` of ``tasks``, started and then ``run``, ended, recorded as its end.
    Stopped on its own, it ends as ``stop`` says; when the lane itself is stopping, it ends as
    cancelled and the cancellation goes on. A failure of the tree, of the host agents reach orbit
    through, of making a temporary directory for the agent or of saving the config is published
    with ``publish``; anything else is a bug."""
    default = ended("cancelled", (CANCELLED, ""))  # What the lane stopping leaves it as.
    return await see_to_end(
        tasks, task_id, default, _run_from_start(tasks, task_id, run, stop, publish)
    )


async def _run_from_start(
    tasks: Tasks,
    task_id: str,
    run: Callable[[], Awaitable[Outcome]],
    stop: UserStop,
    publish: Publish,
) -> Outcome:
    """How task ``task_id`` of ``tasks``, from its start, ended; anything unexpected escapes."""
    try:
        await tasks.start(task_id)
        return await run()
    except asyncio.CancelledError:
        await stop.cleanup()
        if cancelling():  # The lane itself is stopping, not just this task.
            raise
        return ended("cancelled", (stop.reason, ""))
    except TreeFailed as exc:
        return await tree_failed(publish, exc)
    except (HostFailed, TempDirFailed, ConfigWriteFailed) as exc:
        return await run_failed(publish, exc)
