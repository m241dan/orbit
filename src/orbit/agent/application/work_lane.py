"""The work lane: the board's tasks, run one at a time through a new agent each.

After each task that completes, the verification steps run until one fails; a failure adds a
task to fix it right after the failed one. Work that passes lands, and the review lane, if any,
hears how each task ended.
"""

import asyncio
from collections.abc import Collection
from dataclasses import replace

from orbit.agent.application.agent_run import AgentDesk, AgentRun, Outcome
from orbit.agent.application.landing import Lander
from orbit.agent.application.review_lane import ReviewLane
from orbit.agent.application.startup_check import StartupCheck
from orbit.agent.application.verifier import Verifier
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.commits import commit_message, configure_message
from orbit.agent.domain.roles import REVIEW_ROLES, ChecksProposal
from orbit.agent.domain.verification import FixAttempts, VerificationStep, fix_prompt
from orbit.protocol import (
    ErrorOccurred,
    TasksChanged,
    TaskSpec,
    TaskStarted,
    TaskStatus,
)


class WorkLane:
    """Runs ``board``'s tasks through ``desk``'s agents, checked by ``verifier`` if any.

    Reviews are left to ``review``, when there's a review lane. :meth:`run` is meant to run as
    a task of its own: cancelling it cancels the task running, and the run ends there.
    """

    def __init__(
        self,
        desk: AgentDesk,
        board: TaskBoard,
        verifier: Verifier | None,
        review: ReviewLane | None,
    ) -> None:
        self._desk = desk
        self._board = board
        self._verifier = verifier
        self._publish = desk.publish
        self._review = review
        self.startup: StartupCheck | None = None  # Whose panel each task takes over.
        self._landed: dict[str, str] = {}  # Task id -> the commit it landed, until reported.
        self._starts: dict[str, str] = {}  # Task id -> the tree's head when it started.

    @property
    def _checks(self) -> Verifier:
        assert self._verifier is not None  # Only used while verifying a task.
        return self._verifier

    def next_work(self, tried: Collection[str] = ()) -> str | None:
        """The next task for the work lane; reviews have their own lane, if there is one."""
        exclude = REVIEW_ROLES if self._review is not None else ()
        return self._board.next_runnable(skip=tried, exclude=exclude)

    async def run(self) -> None:
        """Run the tasks waiting, in order, until none are left or one is cancelled."""
        tried: set[str] = set()
        attempts = FixAttempts()
        while (task_id := self.next_work(tried)) is not None:
            tried.add(task_id)
            if await self._run_task(task_id, attempts) == "cancelled":
                return

    async def _run_task(self, task_id: str, attempts: FixAttempts) -> TaskStatus:
        """Run one task through a new agent; returns its final status."""
        self._board.set_status(task_id, "running")
        await self._publish_tasks()
        if self.startup is not None:
            self.startup.release_panel()
        if self._verifier:
            self._board.reset_steps()
            await self._checks.publish_steps()
        await self._publish(TaskStarted(task_id=task_id))
        try:
            status = await self._run_agent(self._desk, task_id, attempts)
        except asyncio.CancelledError:
            if self._verifier:
                await self._verifier.cancel()
            status = "cancelled"
        except RuntimeError as exc:  # Git or the tree's setup failed.
            await self._publish(ErrorOccurred(message=str(exc)))
            status = "error"
        self._board.finish(task_id, status)
        await self._publish_tasks()
        await self._report_to_review(task_id)
        return status

    async def _report_to_review(self, task_id: str) -> None:
        """Tell the review lane how task ``task_id`` ended."""
        if self._review is None:
            return
        commit = self._landed.pop(task_id, None)
        if commit is not None:
            await self._review.landed(task_id, commit)
        else:
            await self._review.ended(task_id)

    async def _run_agent(self, desk: AgentDesk, task_id: str, attempts: FixAttempts) -> TaskStatus:
        """Run task ``task_id`` through a new agent and land its work; a fallback queues a fix
        for a fresh one."""
        root = await self._prepare(desk, task_id)
        task = self._board.get(task_id).task
        before = await self._verifier.state_before(task, root) if self._verifier else None
        outcome = await AgentRun(task_id, self._board, self._verifier, desk).run(before, root)
        if outcome.checks is not None:
            await self._configure(desk, outcome.checks, root)
        if outcome.status == "completed":
            outcome = await self._land(desk, task_id, outcome, root)
        if outcome.failure is not None and outcome.status == "unverified":
            await self._queue_fix(task_id, attempts, *outcome.failure)
        return outcome.status

    async def _configure(self, desk: AgentDesk, checks: ChecksProposal, root: str) -> None:
        """Save the ``checks`` the user accepted into the config at ``root``, and use them from
        the next task on. This, or a restart, is the only way the checks change."""
        if desk.agents.write_config is not None:
            desk.agents.write_config(root, checks)
        if desk.agents.tree is not None:
            desk.agents.tree.use_setup(checks.setup)
        self._checks.verification = replace(self._checks.verification, steps=checks.steps)
        self._board.set_steps([step.name for step in checks.steps])
        await self._checks.publish_steps()

    async def _land(self, desk: AgentDesk, task_id: str, outcome: Outcome, root: str) -> Outcome:
        """Commit and merge a completed task's work, when there's a tree to do it from: the
        checks the user accepted from it, or else the work itself."""
        if desk.agents.tree is None:
            return outcome
        if outcome.checks is not None:
            message = configure_message(outcome.checks)
        else:
            message = commit_message(self._board.get(task_id).task, outcome.summaries)
        lander = Lander(desk.agents.tree, self._board, self._verifier, self._publish)
        landing = await lander.land(task_id, message, root, self._starts.pop(task_id))
        if landing.failure is not None:
            return Outcome("unverified", landing.failure)
        if landing.commit is not None:
            self._landed[task_id] = landing.commit
        return outcome

    async def _prepare(self, desk: AgentDesk, task_id: str) -> str:
        """Where task ``task_id`` is worked on: the coder's tree, readied for it."""
        if desk.agents.tree is None:
            return "."
        # A fix carries on from the work it fixes.
        tree = desk.agents.tree
        root = await tree.prepare(keep=self._board.get(task_id).fixes is not None)
        self._starts[task_id] = await tree.head()
        return root

    async def _queue_fix(
        self, task_id: str, attempts: FixAttempts, step: VerificationStep, output: str
    ) -> None:
        """Add a task fixing ``step`` right after ``task_id``, unless its attempts ran out."""
        limit = self._checks.verification.max_fix_attempts
        if attempts.exhausted(task_id, step.name, limit):
            message = f"Verification step {step.name!r} still fails after {limit} fix attempts."
            await self._publish(ErrorOccurred(message=message))
            return
        settings = self._board.get(task_id).task.settings
        fix = TaskSpec(prompt=fix_prompt(step, output), settings=settings)
        fix_id = self._board.add(fix, fixes=task_id)
        attempts.record(fix_id, task_id, step.name)
        if self._review is not None:
            self._review.joined(fix_id)

    async def _publish_tasks(self) -> None:
        await self._publish(TasksChanged(tasks=self._board.tasks))
