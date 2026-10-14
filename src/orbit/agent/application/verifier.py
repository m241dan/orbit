"""Verifying a task once its agent is done: which steps run, and reporting their progress."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from orbit.agent.application.ports import CoderTree, StepRunner, Workspace
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.verification import (
    MAX_FIX_ATTEMPTS,
    VerificationStep,
    may_change_files,
    skip_reason,
)
from orbit.protocol import (
    Event,
    Notice,
    TasksChanged,
    TaskSpec,
    VerificationChanged,
    VerificationFinished,
    VerificationSkipped,
    VerificationStarted,
)

T = TypeVar("T")

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
        tree: CoderTree | None = None,
    ) -> None:
        self.verification = verification
        self._board = board
        self._publish = publish
        self._tree = tree

    async def state_before(self, task: TaskSpec, root: str) -> object:
        """The tree's state at ``root``, when it will tell whether ``task`` changed files."""
        if not may_change_files(task):
            return None
        return await self.fingerprint(root)

    async def check(self, task_id: str, before: object, root: str) -> Failure | None:
        """Verify task ``task_id`` at ``root``, unless there's nothing to verify; returns the
        first failure.

        ``before`` is what :meth:`state_before` returned when the task started.
        """
        task = self._board.get(task_id).task
        after = None if before is None else await self.fingerprint(root)
        reason = skip_reason(task, before, after)
        if reason is not None:
            self._board.skip_steps()
            await self.publish_steps()
            await self._publish(VerificationSkipped(task_id=task_id, reason=reason))
            return None
        self._board.set_status(task_id, "verifying")
        await self._publish(TasksChanged(tasks=self._board.tasks))
        return await self._tidily(self._verify(task_id, root))

    async def run_one(self, step: VerificationStep, root: str) -> tuple[bool, str]:
        """Run ``step`` at ``root`` on its own, as an agent asked; returns its result."""
        return await self._tidily(self.verification.run_step(step, root))

    async def _tidily(self, checks: Awaitable[T]) -> T:
        """Run ``checks`` in the coder's tree, then have git ignore files they created."""
        if self._tree is None:
            return await checks
        before = await self._tree.untracked()
        result = await checks
        created = await self._tree.untracked() - before
        if created:
            await self._tree.ignore(created)
            files = ", ".join(sorted(created))
            message = (
                f"Orbit's checks created files git doesn't ignore ({files}); orbit now ignores"
                " them in .git/info/exclude. Consider adding them to .gitignore."
            )
            await self._publish(Notice(message=message))
        return result

    async def fingerprint(self, root: str) -> object:
        """The state of the tree at ``root`` now; ``None`` when it can't be told."""
        workspace = self.verification.workspace
        return None if workspace is None else await workspace(root)

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

    async def _verify(self, task_id: str, root: str) -> Failure | None:
        """Run the steps at ``root`` after task ``task_id``; returns the first failure."""
        for number, step in enumerate(self.verification.steps):
            self._board.set_step(number, "running")
            await self.publish_steps()
            await self._publish(VerificationStarted(task_id=task_id, step=number))
            passed, output = await self.verification.run_step(step, root)
            self._board.set_step(number, "passed" if passed else "failed")
            await self.publish_steps()
            await self._publish(VerificationFinished(task_id=task_id, step=number, passed=passed))
            if not passed:
                return step, output
        return None
