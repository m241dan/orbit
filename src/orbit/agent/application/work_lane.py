"""The work lane: the board's tasks, run one at a time through a new agent each.

After each task that completes, the verification steps run until one fails; a failure adds a
task to fix it right after the failed one. Work that passes lands, and the review lane, if any,
hears how each task ended. A run stops at a task its agent couldn't do for Claude's usage limit,
at a fix that ends in error, which must run before anything puts its work aside, and once the
session halts.
"""

import asyncio
from collections.abc import Collection
from dataclasses import dataclass, field
from functools import partial
from typing import Literal

from orbit.agent.application.agent_run import AgentRun
from orbit.agent.application.coder_slots import CoderSeat
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.landing import Lander
from orbit.agent.application.lanes import OPEN_GATE, SessionGate, stop
from orbit.agent.application.outcomes import Outcome, UserStop, ended, run_task_to_end
from orbit.agent.application.review_lane import ReviewLane
from orbit.agent.application.startup_check import StartupCheck
from orbit.agent.application.task_trace import TaskTracing
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.verifier import Failure, Verifier
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.checks import ChecksProposal
from orbit.agent.domain.commits import commit_message, configure_message
from orbit.agent.domain.endings import (
    STOPPED_BY_YOU,
    WORK_GONE,
    gave_up,
)
from orbit.agent.domain.roles import CODER_ROLES
from orbit.agent.domain.usage_limits import FIX_ERROR_NOTE, limit_hit
from orbit.agent.domain.verification import FallbackFixes, failure_reason, fix_prompt
from orbit.protocol import (
    CHECKS_EXHAUSTED,
    REVIEW_ROLES,
    ErrorOccurred,
    Notice,
    TaskSpec,
    TaskStarted,
    TaskState,
)

RunEnd = Literal["finished", "cancelled", "limited", "fix_failed"]
"""Why a run ended: no task was left to run, one was cancelled, an agent hit Claude's usage
limit, or a fix ended in error."""


@dataclass(frozen=True)
class Beside:
    """What runs beside the work lane: the review lane, if any, and the startup check, if any,
    whose panel each task takes over; the session's ``gate``, which stops a run halted before
    its next task; its seat at the ``coders``' slots, if any, which a coder's or
    configurator's task takes one of before it starts; and the ``tracing`` of its tasks."""

    review: ReviewLane | None = None
    startup: StartupCheck | None = None
    gate: SessionGate = OPEN_GATE
    coders: CoderSeat | None = None
    tracing: TaskTracing = field(default_factory=TaskTracing)


class WorkLane:
    """Runs ``tasks``' tasks through ``desk``'s agents, checked by ``verifier`` if any.

    Reviews are left to ``beside``'s review lane, when there's one. :meth:`run` is meant to run as
    a task of its own: cancelling it cancels the task running, and the cancel goes on once that
    task has ended. The user's stop, :meth:`stopped_by_user`, cancels only the agent's run, and
    the run ends by itself. A coder's or configurator's task takes a slot at ``beside``'s
    coders, waiting in line for one, before it starts, and gives it back once it has ended,
    however it does; the user's stop leaves the line. Each task is traced, from before it
    waits for a slot until its status is final, with ``beside``'s tracing.
    """

    def __init__(
        self,
        desk: AgentDesk,
        tasks: Tasks,
        verifier: Verifier | None,
        beside: Beside,
    ) -> None:
        self._desk = desk
        self._tasks = tasks
        self._board = tasks.board
        self._verifier = verifier
        self._publish = desk.publish
        self._review = beside.review
        self._startup = beside.startup
        self._gate = beside.gate
        self._coders = beside.coders
        self._tracing = beside.tracing
        self._seated = False  # The run holds a coder's slot, for the task about to run or running.
        # Task id -> the commit it merged into the base, until reported.
        self._landed: dict[str, str] = {}
        self._starts: dict[str, str] = {}  # Task id -> the tree's head when it started.
        # Fixes queued per step along each chain, kept across runs so a restart can't reset
        # the cap.
        self._fallbacks = FallbackFixes()
        self._running = False  # A run is going.
        self._stopped = False  # The user stopped the run going now.
        self._agent: asyncio.Task[Outcome] | None = None  # The agent's run of the current task.
        self._current: str | None = None  # The task running, until the review lane hears of it.
        # The task a run stopped at, which the next one runs first: a fix must run before
        # another task puts its work aside.
        self._resume: str | None = None

    def reset(self) -> None:
        """Forget the fix counts, the task a run stopped at and what its tasks landed and
        started from, of the session left, whose task ids the next one reuses."""
        self._fallbacks = FallbackFixes()
        self._resume = None
        self._landed.clear()
        self._starts.clear()

    def stopped_by_user(self) -> bool:
        """Stop the run going now, as the user asked: cancel the agent's run of its task, which
        that task says, and run no other; returns whether a run was going."""
        if not self._running:
            return False
        self._stopped = True
        if self._coders is not None:
            self._coders.drop()
        if self._agent is not None:
            self._agent.cancel()
        return True

    @property
    def current(self) -> str | None:
        """The task the run is on or, before it picks one, the one it will pick; none when
        there's none."""
        return self._current or self.next_work()

    @property
    def waiting_for_coder(self) -> int | None:
        """Where the run waits in line for a coder's slot, 1 next; none if it doesn't."""
        return None if self._coders is None else self._coders.position

    @property
    def coders_busy(self) -> int | None:
        """How many coders hold the slots the run waits in line for; none if it doesn't wait."""
        coders = self._coders
        if coders is None or coders.position is None:
            return None
        return coders.busy

    def next_work(self, tried: Collection[str] = ()) -> str | None:
        """The next task for the work lane, the one a run stopped at first; reviews have their
        own lane, if there is one."""
        exclude = REVIEW_ROLES if self._review is not None else ()
        return self._board.next_runnable(skip=tried, exclude=exclude, first=self._resume)

    async def run(self) -> RunEnd:
        """Run the tasks waiting, in order but the one the last run stopped at first, until
        none are left, the user stops one, the session halts, which ends it as cancelled, or one
        ends in error for the usage limit or as a fix; returns which. Any other cancel goes on
        once the task running has ended."""
        self._running, self._stopped = True, False
        try:
            return await self._run_tasks()
        finally:
            self._running = False

    async def _run_tasks(self) -> RunEnd:
        """Run the tasks waiting, as :meth:`run` does; returns why the run ended."""
        tried: set[str] = set()
        try:
            while (task_id := self.next_work(tried)) is not None:
                if self._gate.halted():  # It may have halted as the last task ended.
                    return "cancelled"
                with self._tracing.task(self._board, task_id, self._model(task_id)):
                    end = await self._take(task_id, tried)
                if end is not None:
                    return end
            return "finished"
        finally:
            self._unseat()  # A slot kept for a task that never ran.

    def _model(self, task_id: str) -> str | None:
        """The model task ``task_id`` is launched with, the one its own settings name; none if
        they name none, as its role's model is a default for tasks orbit queues, not a
        fallback at launch."""
        return self._board.get(task_id).task.settings.model

    async def _take(self, task_id: str, tried: set[str]) -> RunEnd | None:
        """Seat task ``task_id`` and run it, as :meth:`_run_seated` does; the run ends cancelled
        if the user stopped it as it waited for a slot, and goes on, picking its next task
        again, if the board stopped needing one. A slot is given back once a task has run, and
        kept for the next task if this one didn't."""
        if not await self._seat(task_id, tried):
            return "cancelled" if self._stopped else None
        try:
            return await self._run_seated(task_id, tried)
        finally:
            if task_id in tried:
                self._unseat()

    async def _seat(self, task_id: str, tried: set[str]) -> bool:
        """Take a coder's slot for task ``task_id``, waiting in line for one, if its role takes
        one and no slot is held already, or else give back any held; returns whether it may
        run, false if the run's place in line was dropped: the user stopped the run, or the
        board's next task, among those not ``tried``, no longer takes a slot. The watch on the
        board that drops the place has ended, however this ends; its failure, a bug, escapes."""
        coders = self._coders
        if coders is None or not self._takes_slot(task_id):
            self._unseat()
            return True
        if self._seated:
            return True
        watch = asyncio.create_task(self._leave_line_unneeded(coders, tried))
        try:
            self._seated = await self._tracing.seat(coders)
        finally:
            await stop(watch)
        return self._seated

    async def _leave_line_unneeded(self, coders: CoderSeat, tried: set[str]) -> None:
        """Drop the run's place in line at ``coders`` once the board's next task, among those
        not ``tried``, takes no slot."""
        while self._takes_slot(self.next_work(tried)):
            await self._tasks.moved()
        coders.drop()

    def _takes_slot(self, task_id: str | None) -> bool:
        """Whether task ``task_id``, if any, is of a role that takes a coder's slot."""
        return task_id is not None and self._board.get(task_id).task.role in CODER_ROLES

    def _unseat(self) -> None:
        """Give back the coder's slot held, if any."""
        if self._seated:
            assert self._coders is not None  # Only a seat takes slots.
            self._seated = False
            self._coders.release()

    async def _run_seated(self, task_id: str, tried: set[str]) -> RunEnd | None:
        """Run task ``task_id``, noted among those ``tried``, unless the session halted or the
        board changed as it waited for a slot, then why the run ends, as :meth:`_end_at` says;
        none if the run goes on, picking its next task again."""
        if self._gate.halted() or self.next_work(tried) != task_id:
            return None
        tried.add(task_id)
        self._resume = None
        fixing = self._board.get(task_id).fixes is not None
        outcome = await self._run_task(task_id)
        return await self._end_at(task_id, outcome, fixing)

    async def _end_at(self, task_id: str, outcome: Outcome, fixing: bool) -> RunEnd | None:
        """Why the run ends at task ``task_id``, which ended in ``outcome``, ``fixing`` another
        or not: it was cancelled or stopped, or the run stops at its error, to run it first
        next time; none if the run goes on."""
        if outcome.status == "cancelled" or self._stopped:
            return "cancelled"
        if outcome.status == "error" and (end := await self._stop_at(outcome, fixing)):
            self._resume = task_id
            return end
        return None

    async def _stop_at(self, error: Outcome, fixing: bool) -> RunEnd | None:
        """Why the run stops at a task that ended in ``error``: its agent hit the usage limit,
        or it was ``fixing`` another, said so; none if the run goes on."""
        if limit_hit(error.detail):
            return "limited"
        if not fixing:
            return None
        await self._publish(Notice(message=FIX_ERROR_NOTE))
        return "fix_failed"

    async def _run_task(self, task_id: str) -> Outcome:
        """Run one task through a new agent, as the :attr:`current` one; returns how it
        ended."""
        self._current = task_id
        try:
            return await self._run_current(task_id)
        finally:
            self._current = None

    async def _run_current(self, task_id: str) -> Outcome:
        """Run task ``task_id`` through a new agent, then tell the review lane how it ended;
        returns how it did. The lane stopping tells the review lane only of work that landed,
        which must always be reviewed."""
        run = partial(self._run_alone, task_id)
        stop = UserStop(STOPPED_BY_YOU, self._cancelled)
        state = self._board.get(task_id)  # Taken now: the task may be removed once it finishes.
        outcome: Outcome | None = None
        try:
            outcome = await run_task_to_end(self._tasks, task_id, run, stop, self._publish)
            return outcome
        finally:
            await self._report_to_review(state, outcome)

    async def _run_alone(self, task_id: str) -> Outcome:
        """How started task ``task_id``'s run through an agent ended, run as a task of its own
        that the user's stop cancels, leaving the run going; anything unexpected escapes."""
        agent = asyncio.create_task(self._run_started(task_id))
        if self._stopped:  # The user stopped the run as the task started.
            agent.cancel()
        self._agent = agent
        try:
            return await agent
        finally:
            self._agent = None

    async def _run_started(self, task_id: str) -> Outcome:
        """How started task ``task_id``'s run through an agent ended; anything unexpected
        escapes."""
        await self._start(task_id)
        return await self._run_agent(task_id)

    async def _cancelled(self) -> None:
        """The task running was cancelled: mark the step in progress cancelled."""
        if self._verifier is not None:
            await self._verifier.cancel()

    async def _start(self, task_id: str) -> None:
        """Say task ``task_id``, marked running, started, with every verification step pending
        again."""
        if self._startup is not None:
            self._startup.release_panel()
        if self._verifier is not None:
            await self._verifier.reset()
        await self._publish(TaskStarted(task_id=task_id))

    async def _report_to_review(self, state: TaskState, outcome: Outcome | None) -> None:
        """Tell the review lane the commit the task that was ``state`` merged, if any, or else,
        if it finished in ``outcome`` rather than being cut short, that it ended. The task is
        told of as it ended, without looking it up: a task is published as finished before the
        save that follows, and one removed in that window is reviewed all the same."""
        commit = self._landed.pop(state.id, None)
        if self._review is None:
            return
        if outcome is not None:
            state = state.model_copy(update={"status": outcome.status})
        if commit is not None:
            await self._review.landed(state, commit)
        elif outcome is not None:
            await self._review.ended(state)

    async def _run_agent(self, task_id: str) -> Outcome:
        """Run task ``task_id`` through a new agent and land its work; a fallback queues a fix
        for a fresh one."""
        root = await self._prepare(task_id)
        try:
            return await self._run_prepared(task_id, root)
        finally:
            self._starts.pop(task_id, None)

    async def _run_prepared(self, task_id: str, root: str) -> Outcome:
        """Run task ``task_id``, its tree readied at ``root``, through a new agent and land its
        work, as :meth:`_run_agent` does."""
        if not await self._holds_fixed_work(task_id):
            return ended("gave_up", gave_up(WORK_GONE))
        task = self._board.get(task_id).task
        verifier = self._verifier
        before = await verifier.state_before(task, root) if verifier is not None else None
        with self._tracing.run():
            outcome = await AgentRun(task_id, self._tasks, verifier, self._desk).run(before, root)
            return await self._settle(task_id, outcome, root)

    async def _settle(self, task_id: str, outcome: Outcome, root: str) -> Outcome:
        """What task ``task_id``'s run ending in ``outcome`` at ``root`` comes to: the checks
        accepted from it are used, its completed work lands, and a fallback queues a fix."""
        verifier = self._verifier
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
        raises :class:`ConfigWriteFailed` before anything changes, ending the task in error."""
        if self._desk.agents.write_config is not None:
            await asyncio.to_thread(self._desk.agents.write_config, root, checks)
        if self._desk.agents.tree is not None:
            self._desk.agents.tree.use_setup(checks.setup)
        await verifier.use_steps(checks.steps)

    async def _land(self, task_id: str, outcome: Outcome, root: str) -> Outcome:
        """Commit and merge a completed task's work, when there's a tree to do it from: the
        checks the user accepted from it, or else the work itself. A rebase is rechecked only
        if the task's role is verified. The work has landed, for review, only once it merged:
        the task stays completed either way, with the merge's detail."""
        if self._desk.agents.tree is None:
            return outcome
        state = self._board.get(task_id)
        if outcome.checks is not None:
            message = configure_message(outcome.checks)
        else:
            message = commit_message(state.task, outcome.summaries, fix=state.fixes is not None)
        verifier = self._verifier if self._desk.agents.roles[state.task.role].verify else None
        lander = Lander(self._desk.agents.tree, verifier, self._publish)
        landing = await lander.land(task_id, message, root, self._starts[task_id])
        if landing.failure is not None:
            reason, detail = failure_reason(*landing.failure, rebased=True)
            return Outcome("unverified", landing.failure, reason=reason, detail=detail)
        if landing.merged is not None:
            self._landed[task_id] = landing.merged
        return outcome

    async def _prepare(self, task_id: str) -> str:
        """Where task ``task_id`` is worked on: the coder's tree, readied for it."""
        if self._desk.agents.tree is None:
            return self._desk.agents.root
        # A fix carries on from the work it fixes.
        tree = self._desk.agents.tree
        root = await tree.prepare(keep=self._board.get(task_id).fixes is not None)
        self._starts[task_id] = await tree.head()
        return root

    async def _holds_fixed_work(self, task_id: str) -> bool:
        """Whether the coder's tree, if there is one, still holds the work task ``task_id``
        fixes, if it's a fix: work put aside since is in a stash, and its checks would pass on
        the base without it."""
        tree = self._desk.agents.tree
        if tree is None or self._board.get(task_id).fixes is None:
            return True
        return await tree.holds_work()

    async def _queue_fix(self, verifier: Verifier, task_id: str, failure: Failure) -> None:
        """Add a task fixing ``failure``'s step right after ``task_id``, unless its attempts
        ran out."""
        step, output = failure
        limit = verifier.verification.fallback_fixes
        if self._fallbacks.exhausted(task_id, step.name, limit):
            message = f"Verification step {step.name!r} still fails after {limit} fix attempts."
            await self._publish(ErrorOccurred(code=CHECKS_EXHAUSTED, message=message))
            return
        settings = self._board.get(task_id).task.settings
        fix = TaskSpec(prompt=fix_prompt(step, output), settings=settings)

        def add(board: TaskBoard) -> None:
            fix_id = board.add(fix, fixes=task_id)
            self._fallbacks.record(fix_id, task_id, step.name)
            if self._review is not None:
                self._review.joined(fix_id)

        await self._tasks.change(add)
