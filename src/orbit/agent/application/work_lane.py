"""The work lane: the board's tasks, run one at a time through a new agent each.

After each task that completes, the verification steps run until one fails; a failure adds a
task to fix it right after the failed one. Work that passes lands, and the review lane, if any,
hears how each task ended.
"""

import asyncio
from collections.abc import Collection

from orbit.agent.application.agent_run import AgentRun, Outcome, ended
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.landing import Lander
from orbit.agent.application.review_lane import ReviewLane
from orbit.agent.application.startup_check import StartupCheck
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.verifier import Failure, Verifier
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.commits import commit_message, configure_message
from orbit.agent.domain.endings import BUG, CANCELLED, STOPPED_BY_YOU, bug, error
from orbit.agent.domain.roles import REVIEW_ROLES, ChecksProposal
from orbit.agent.domain.verification import FallbackFixes, failure_reason, fix_prompt
from orbit.protocol import (
    ErrorOccurred,
    TaskSpec,
    TaskStarted,
    TaskStatus,
)


class WorkLane:
    """Runs ``tasks``' tasks through ``desk``'s agents, checked by ``verifier`` if any.

    Reviews are left to ``review``, when there's a review lane. :meth:`run` is meant to run as
    a task of its own: cancelling it cancels the task running, and the run ends there.
    """

    def __init__(
        self,
        desk: AgentDesk,
        tasks: Tasks,
        verifier: Verifier | None,
        review: ReviewLane | None,
    ) -> None:
        self._desk = desk
        self._tasks = tasks
        self._board = tasks.board
        self._verifier = verifier
        self._publish = desk.publish
        self._review = review
        self.startup: StartupCheck | None = None  # Whose panel each task takes over.
        self._landed: dict[str, str] = {}  # Task id -> the commit it landed, until reported.
        self._starts: dict[str, str] = {}  # Task id -> the tree's head when it started.
        # Fixes queued per step along each chain, kept across runs so a restart can't reset
        # the cap.
        self._fallbacks = FallbackFixes()
        self._stopped = False  # The user stopped the run going now.

    def reset(self) -> None:
        """Forget the fix counts of the session left, whose task ids the next one reuses."""
        self._fallbacks = FallbackFixes()

    def stopped_by_user(self) -> None:
        """The run going now is being cancelled because the user stopped it, which the task it
        cancels says."""
        self._stopped = True

    def next_work(self, tried: Collection[str] = ()) -> str | None:
        """The next task for the work lane; reviews have their own lane, if there is one."""
        exclude = REVIEW_ROLES if self._review is not None else ()
        return self._board.next_runnable(skip=tried, exclude=exclude)

    async def run(self) -> None:
        """Run the tasks waiting, in order, until none are left or one is cancelled."""
        tried: set[str] = set()
        self._stopped = False
        while (task_id := self.next_work(tried)) is not None:
            tried.add(task_id)
            if await self._run_task(task_id) == "cancelled":
                return

    async def _run_task(self, task_id: str) -> TaskStatus:
        """Run one task through a new agent; returns its final status."""
        await self._tasks.start(task_id)
        if self.startup is not None:
            self.startup.release_panel()
        if self._verifier is not None:
            await self._verifier.reset()
        await self._publish(TaskStarted(task_id=task_id))
        outcome = Outcome("error", reason=BUG)  # What a bug in the run leaves the task as.
        try:
            outcome = await self._outcome(task_id)
        except Exception as exc:
            outcome = ended("error", bug(exc))
            raise
        finally:  # Even a bug ends the task, or the board could never run or drop it.
            await self._tasks.finish(task_id, outcome.status, outcome.reason, outcome.detail)
        await self._report_to_review(task_id)
        return outcome.status

    async def _outcome(self, task_id: str) -> Outcome:
        """How task ``task_id``'s run through an agent ended; anything unexpected escapes."""
        try:
            return await self._run_agent(task_id)
        except asyncio.CancelledError:
            if self._verifier is not None:
                await self._verifier.cancel()
            # Read after the await: the user may have stopped the run meanwhile.
            return ended("cancelled", (STOPPED_BY_YOU if self._stopped else CANCELLED, ""))
        except RuntimeError as exc:  # Git or the tree's setup failed.
            await self._publish(ErrorOccurred(message=str(exc)))
            return ended("error", error(str(exc)))

    async def _report_to_review(self, task_id: str) -> None:
        """Tell the review lane how task ``task_id`` ended."""
        if self._review is None:
            return
        commit = self._landed.pop(task_id, None)
        if commit is not None:
            await self._review.landed(task_id, commit)
        else:
            await self._review.ended(task_id)

    async def _run_agent(self, task_id: str) -> Outcome:
        """Run task ``task_id`` through a new agent and land its work; a fallback queues a fix
        for a fresh one."""
        root = await self._prepare(task_id)
        task = self._board.get(task_id).task
        verifier = self._verifier
        before = await verifier.state_before(task, root) if verifier is not None else None
        outcome = await AgentRun(task_id, self._tasks, verifier, self._desk).run(before, root)
        if outcome.checks is not None:
            assert verifier is not None  # Checks are only proposed to a verifier.
            await self._configure(verifier, outcome.checks, root)
        if outcome.status == "completed":
            outcome = await self._land(task_id, outcome, root)
        if outcome.failure is not None and outcome.status == "unverified":
            assert verifier is not None  # Only verification fails.
            await self._queue_fix(verifier, task_id, outcome.failure)
        return outcome

    async def _configure(self, verifier: Verifier, checks: ChecksProposal, root: str) -> None:
        """Save the ``checks`` the user accepted into the config at ``root``, and use them from
        the next task on. This, or a restart, is the only way the checks change. A failed save
        raises ``RuntimeError`` before anything changes, ending the task in error."""
        if self._desk.agents.write_config is not None:
            await asyncio.to_thread(self._desk.agents.write_config, root, checks)
        if self._desk.agents.tree is not None:
            self._desk.agents.tree.use_setup(checks.setup)
        await verifier.use_steps(checks.steps)

    async def _land(self, task_id: str, outcome: Outcome, root: str) -> Outcome:
        """Commit and merge a completed task's work, when there's a tree to do it from: the
        checks the user accepted from it, or else the work itself. A rebase is rechecked only
        if the task's role is verified."""
        if self._desk.agents.tree is None:
            return outcome
        state = self._board.get(task_id)
        if outcome.checks is not None:
            message = configure_message(outcome.checks)
        else:
            message = commit_message(state.task, outcome.summaries, fix=state.fixes is not None)
        verifier = self._verifier if self._desk.agents.roles[state.task.role].verify else None
        lander = Lander(self._desk.agents.tree, verifier, self._publish)
        landing = await lander.land(task_id, message, root, self._starts.pop(task_id))
        if landing.failure is not None:
            reason, detail = failure_reason(*landing.failure, rebased=True)
            return Outcome("unverified", landing.failure, reason=reason, detail=detail)
        if landing.commit is not None:
            self._landed[task_id] = landing.commit
        return outcome

    async def _prepare(self, task_id: str) -> str:
        """Where task ``task_id`` is worked on: the coder's tree, readied for it."""
        if self._desk.agents.tree is None:
            return "."
        # A fix carries on from the work it fixes.
        tree = self._desk.agents.tree
        root = await tree.prepare(keep=self._board.get(task_id).fixes is not None)
        self._starts[task_id] = await tree.head()
        return root

    async def _queue_fix(self, verifier: Verifier, task_id: str, failure: Failure) -> None:
        """Add a task fixing ``failure``'s step right after ``task_id``, unless its attempts
        ran out."""
        step, output = failure
        limit = verifier.verification.fallback_fixes
        if self._fallbacks.exhausted(task_id, step.name, limit):
            message = f"Verification step {step.name!r} still fails after {limit} fix attempts."
            await self._publish(ErrorOccurred(message=message))
            return
        settings = self._board.get(task_id).task.settings
        fix = TaskSpec(prompt=fix_prompt(step, output), settings=settings)

        def add(board: TaskBoard) -> None:
            fix_id = board.add(fix, fixes=task_id)
            self._fallbacks.record(fix_id, task_id, step.name)
            if self._review is not None:
                self._review.joined(fix_id)

        await self._tasks.change(add)
