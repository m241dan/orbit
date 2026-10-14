"""Verifying a task once its agent is done: which steps run, and reporting their progress."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from orbit.agent.application.ports import StepRunner, Workspace
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.verification import (
    MAX_FIX_ATTEMPTS,
    VerificationStep,
    may_change_files,
    skip_reason,
)
from orbit.protocol import (
    Event,
    TasksChanged,
    TaskSpec,
    VerificationChanged,
    VerificationFinished,
    VerificationSkipped,
    VerificationStarted,
)

Failure = tuple[VerificationStep, str]
"""The step that failed and its output."""


@dataclass(frozen=True)
class Verification:
    """How tasks are checked once their turn completes.

    ``steps`` run in order through ``run_step`` until one fails. ``workspace`` tells
    whether a task changed any files; without it, every task counts as changing them.
    A task's chain of fixes gets ``max_fix_attempts`` for each step.
    """

    steps: tuple[VerificationStep, ...]
    run_step: StepRunner
    workspace: Workspace | None = None
    max_fix_attempts: int = MAX_FIX_ATTEMPTS


class Verifier:
    """Runs ``verification``'s steps on tasks of ``board``, publishing progress as it goes."""

    def __init__(
        self,
        verification: Verification,
        board: TaskBoard,
        publish: Callable[[Event], Awaitable[None]],
    ) -> None:
        self.verification = verification
        self._board = board
        self._publish = publish

    async def state_before(self, task: TaskSpec) -> object:
        """The working tree's state, when it will tell whether ``task`` changed files."""
        workspace = self.verification.workspace
        if workspace is None or not may_change_files(task):
            return None
        return await workspace()

    async def check(self, task_id: str, before: object) -> Failure | None:
        """Verify task ``task_id``, unless there's nothing to verify; returns the first failure.

        ``before`` is what :meth:`state_before` returned when the task started.
        """
        task = self._board.get(task_id).task
        reason = skip_reason(task, before, await self._state_after(before))
        if reason is not None:
            self._board.skip_steps()
            await self.publish_steps()
            await self._publish(VerificationSkipped(task_id=task_id, reason=reason))
            return None
        self._board.set_status(task_id, "verifying")
        await self._publish(TasksChanged(tasks=self._board.tasks))
        return await self._verify(task_id)

    async def fingerprint(self) -> object:
        """The working tree's state now; ``None`` when it can't be told."""
        workspace = self.verification.workspace
        return None if workspace is None else await workspace()

    async def skip_unchanged(self, task_id: str) -> None:
        """Report that task ``task_id`` isn't verified again: nothing changed since it failed."""
        self._board.skip_steps()
        await self.publish_steps()
        await self._publish(VerificationSkipped(task_id=task_id, reason="unchanged"))

    async def cancel(self) -> None:
        """The task was cancelled while verifying: mark the step in progress cancelled."""
        self._board.cancel_running_steps()
        await self.publish_steps()

    async def publish_steps(self) -> None:
        await self._publish(VerificationChanged(steps=self._board.steps))

    async def _state_after(self, before: object) -> object:
        if before is None:
            return None  # Nothing to compare with, so don't look.
        workspace = self.verification.workspace
        assert workspace is not None  # It produced ``before``.
        return await workspace()

    async def _verify(self, task_id: str) -> Failure | None:
        """Run the steps after task ``task_id``; returns the first failure and its output."""
        for number, step in enumerate(self.verification.steps):
            self._board.set_step(number, "running")
            await self.publish_steps()
            await self._publish(VerificationStarted(task_id=task_id, step=number))
            passed, output = await self.verification.run_step(step)
            self._board.set_step(number, "passed" if passed else "failed")
            await self.publish_steps()
            await self._publish(VerificationFinished(task_id=task_id, step=number, passed=passed))
            if not passed:
                return step, output
        return None
