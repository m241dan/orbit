"""The startup check: the verification steps, run once on the base commit as orbit starts, so a
broken setup shows up while the user types their first prompt rather than on the first task.

It runs in the background, in a read-only snapshot of the base commit like a review, so it never
races with an agent in the coder's tree. It shows in the verification panel until a task's own
verification takes the panel over, and in the agents pane as an agent that can be cancelled.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from orbit.agent.application.lanes import Background
from orbit.agent.application.ports import CoderTree, Publish, TreeFailed
from orbit.agent.application.snapshots import snapshot_of
from orbit.agent.application.verifier import Failure, Verification, first_failure
from orbit.agent.domain.checks import setup_step
from orbit.agent.domain.verification import StepProgress, VerificationStep, trim_output
from orbit.protocol import (
    GIT_FAILED,
    STARTUP_TASK,
    AgentExited,
    AgentExitReason,
    AgentStarted,
    ErrorOccurred,
    Notice,
    RunningAgent,
    StartupChecked,
    StepStatus,
    VerificationChanged,
)

AGENT_ID = "startup-check"
ROLE = "checker"
TITLE = "Startup check"
BASE = "HEAD"  # The main checkout's commit: the base, as orbit starts.
CANCELLED_NOTE = "Startup check cancelled."

AddFix = Callable[[VerificationStep, str], Awaitable[None]]
"""Adds a draft task to fix the step that failed, given its output."""


@dataclass(frozen=True)
class CheckSite:
    """Where the check runs: in a snapshot of ``tree``'s base, or without a tree in place, in
    ``root``, the workspace's directory."""

    tree: CoderTree | None
    root: str


class StartupCheck:
    """Runs ``verification``'s steps once at ``site``, in a snapshot set up with its tree's
    setup when it has a tree, publishing progress and the result. A failure goes to
    ``add_fix``."""

    def __init__(
        self,
        verification: Verification,
        site: CheckSite,
        publish: Publish,
        add_fix: AddFix,
    ) -> None:
        self._verification = verification
        self._tree = site.tree
        self._root = site.root
        self._publish = publish
        self._add_fix = add_fix
        self._progress = StepProgress.of(step.name for step in verification.steps)
        self._shown = True  # Whether it still has the verification panel.
        self._started = False
        self._announced = False  # Whether its start is published and its exit isn't.
        self._background = Background("startup check", publish)

    def start(self) -> None:
        self._started = True
        self._background.start(self._run())

    def release_panel(self) -> None:
        """A task's verification takes the panel over from now on."""
        self._shown = False

    @property
    def running(self) -> bool:
        return self._background.running

    def on_stopped(self, stopped: Callable[[], None]) -> None:
        """Have ``stopped`` called once the check stops running."""
        self._background.ended = stopped

    @property
    def agent(self) -> RunningAgent | None:
        """The check as a running agent, between publishing its start and its exit."""
        if not self._announced:
            return None
        return RunningAgent(agent_id=AGENT_ID, role=ROLE, task_id=STARTUP_TASK)

    @property
    def panel(self) -> VerificationChanged | None:
        """What it shows in the verification panel; none before it starts, or once a task's
        verification took the panel over."""
        if not self._started or not self._shown:
            return None
        return self._panel()

    def cancel(self) -> None:
        self._background.cancel()

    async def stop(self) -> None:
        """Stop the check, if it runs, and wait for it to."""
        await self._background.stop()

    async def close(self) -> None:
        """Stop the check, if it runs, for good: it doesn't start from now on."""
        await self._background.close()

    async def _run(self) -> None:
        await self._publish(AgentStarted(agent_id=AGENT_ID, role=ROLE, task_id=STARTUP_TASK))
        self._announced = True
        await self._show()
        reason: AgentExitReason = "killed"
        try:
            reason = await self._check()
        finally:
            self._announced = False
            await self._publish(AgentExited(agent_id=AGENT_ID, reason=reason))

    async def _check(self) -> AgentExitReason:
        try:
            failure = await self._in_snapshot()
        except asyncio.CancelledError:
            await self._cancel_running()
            await self._publish(Notice(message=CANCELLED_NOTE))
            raise
        except TreeFailed as exc:  # Git couldn't make the snapshot.
            message = f"The startup check couldn't run: {exc}"
            await self._publish(ErrorOccurred(code=GIT_FAILED, message=message))
            return "crashed"
        await self._report(failure)
        return "finished"

    async def _in_snapshot(self) -> Failure | None:
        if self._tree is None:
            return await self._verify(self._root)
        async with snapshot_of(self._tree, BASE, self._publish) as root:
            return await self._verify(root)

    async def _verify(self, root: str) -> Failure | None:
        """Set up ``root``, then run the steps there; returns the first failure."""
        failure = await self._set_up(root)
        if failure is not None:
            return failure
        verification = self._verification
        return await first_failure(verification.steps, verification.run_step, root, self._set)

    async def _set_up(self, root: str) -> Failure | None:
        """Run the tree's setup at ``root``, if it has one; returns its failure."""
        setup = self._tree.setup if self._tree else None
        if setup is None:
            return None
        return await first_failure((setup_step(setup),), self._verification.run_step, root)

    async def _report(self, failure: Failure | None) -> None:
        if failure is None:
            await self._publish(StartupChecked(passed=True))
            return
        step, output = failure
        await self._add_fix(step, output)
        await self._publish(
            StartupChecked(passed=False, step=step.name, output=trim_output(output))
        )

    async def _cancel_running(self) -> None:
        """Mark the step running, if any, cancelled."""
        cancelled = self._progress.cancel_running()
        if cancelled != self._progress:
            self._progress = cancelled
            await self._show()

    async def _set(self, number: int, status: StepStatus) -> None:
        self._progress = self._progress.set(number, status)
        await self._show()

    async def _show(self) -> None:
        if self._shown:
            await self._publish(self._panel())

    def _panel(self) -> VerificationChanged:
        return VerificationChanged(steps=self._progress.steps, title=TITLE)
