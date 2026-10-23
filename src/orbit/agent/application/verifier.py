"""Verifying a task once its agent is done: which steps run, and reporting their progress."""

import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from typing import TypeVar

from orbit.agent.application.ports import CoderTree, Publish, StepRunner, Workspace
from orbit.agent.application.task_trace import (
    VERIFY_FAILED_STEP,
    VERIFY_RESULT,
    VERIFY_STEP_EVENT,
)
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.verification import (
    MAX_FALLBACK_FIXES,
    StepProgress,
    VerificationStep,
    may_change_files,
    skip_reason,
)
from orbit.protocol import (
    Notice,
    StepStatus,
    TaskSpec,
    VerificationChanged,
    VerificationFinished,
    VerificationSkipped,
    VerificationStarted,
)
from orbit.tracing import Attributes, current_span

T = TypeVar("T")

Failure = tuple[VerificationStep, str]
"""The step that failed and its output."""

OnStep = Callable[[int, StepStatus], Awaitable[None]]
"""Told a step's number and its new status: ``running``, then ``passed`` or ``failed``."""


async def first_failure(
    steps: Iterable[VerificationStep],
    run_step: StepRunner,
    root: str,
    on_step: OnStep | None = None,
) -> Failure | None:
    """Run ``steps`` in order at ``root`` through ``run_step`` until one fails; returns that
    failure. ``on_step``, if given, hears of each step's progress."""
    for number, step in enumerate(steps):
        if on_step is not None:
            await on_step(number, "running")
        passed, output = await run_step(step, root)
        if on_step is not None:
            await on_step(number, "passed" if passed else "failed")
        if not passed:
            return step, output
    return None


@dataclass(frozen=True)
class Verification:
    """How tasks are checked once their turn completes.

    ``steps`` run in order through ``run_step`` until one fails. ``workspace`` tells
    whether a task changed any files; without it, every task counts as changing them.
    A task's chain of fixes may queue ``fallback_fixes`` fix tasks for each step.
    """

    steps: tuple[VerificationStep, ...]
    run_step: StepRunner
    workspace: Workspace | None = None
    fallback_fixes: int = MAX_FALLBACK_FIXES


class Verifier:
    """Runs ``verification``'s steps on ``tasks``, publishing their progress as it
    goes; a task's verification is recorded on the current span, its result and each step's."""

    def __init__(
        self,
        verification: Verification,
        tasks: Tasks,
        publish: Publish,
        tree: CoderTree | None = None,
    ) -> None:
        self.verification = verification
        self._progress = StepProgress.of(step.name for step in verification.steps)
        self._tasks = tasks
        self._board = tasks.board
        self._publish = publish
        self._tree = tree

    @property
    def step_names(self) -> tuple[str, ...]:
        return self._progress.names

    @property
    def progress(self) -> StepProgress:
        return self._progress

    async def use_steps(self, steps: tuple[VerificationStep, ...]) -> None:
        """Verify with ``steps`` from now on, none of them run yet, and report them."""
        self.verification = replace(self.verification, steps=steps)
        self._progress = StepProgress.of(step.name for step in steps)
        await self.publish_steps()

    async def reset(self) -> None:
        """A task starts: report every step pending again."""
        self._progress = self._progress.reset()
        await self.publish_steps()

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
        state = self._board.get(task_id)
        after = None if before is None else await self.fingerprint(root)
        reason = skip_reason(
            state.task,
            before,
            after,
            fixing=state.fixes is not None,
            has_steps=bool(self.verification.steps),
        )
        if reason is not None:
            self._progress = self._progress.skip()
            await self.publish_steps()
            await self._publish(VerificationSkipped(task_id=task_id, reason=reason))
            current_span().set_attributes({VERIFY_RESULT: "skipped"})
            return None
        await self._tasks.change(lambda board: board.set_status(task_id, "verifying"))
        failure = await self._tidily(self._verify(task_id, root))
        _record_result(failure)
        return failure

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
        self._progress = self._progress.skip()
        await self.publish_steps()
        await self._publish(VerificationSkipped(task_id=task_id, reason="unchanged"))

    async def cancel(self) -> None:
        """The task was cancelled while verifying: mark the step in progress cancelled."""
        self._progress = self._progress.cancel_running()
        await self.publish_steps()

    async def publish_steps(self) -> None:
        """Report the steps' progress; there's none to report without steps."""
        if self.verification.steps:
            await self._publish(VerificationChanged(steps=self._progress.steps))

    async def _verify(self, task_id: str, root: str) -> Failure | None:
        """Run the steps at ``root`` after task ``task_id``; returns the first failure."""

        started = time.monotonic()

        async def on_step(number: int, status: StepStatus) -> None:
            nonlocal started
            self._progress = self._progress.set(number, status)
            await self.publish_steps()
            if status == "running":
                started = time.monotonic()
                await self._publish(VerificationStarted(task_id=task_id, step=number))
            else:
                _record_step(self._progress.names[number], status, started)
                passed = status == "passed"
                await self._publish(
                    VerificationFinished(task_id=task_id, step=number, passed=passed)
                )

        return await first_failure(
            self.verification.steps, self.verification.run_step, root, on_step
        )


def _record_step(name: str, result: StepStatus, started: float) -> None:
    """Record on the current span that the step ``name``, run since ``started``, ended as
    ``result``."""
    duration_ms = (time.monotonic() - started) * 1000
    attributes: Attributes = {"name": name, "result": result, "duration_ms": duration_ms}
    current_span().event(VERIFY_STEP_EVENT, attributes)


def _record_result(failure: Failure | None) -> None:
    """Record on the current span whether verification passed, or the step that failed."""
    if failure is None:
        current_span().set_attributes({VERIFY_RESULT: "passed"})
        return
    step, _ = failure
    current_span().set_attributes({VERIFY_RESULT: "failed", VERIFY_FAILED_STEP: step.name})
