"""The work lane: the board's tasks, run one at a time through a new agent each.

After each task that completes, the verification steps run until one fails; a failure adds a
task to fix it right after the failed one. Work that passes lands, and the review lane, if any,
hears how each task ended, as the triggers do: ``task_landed`` for work that landed, raised by
the review lane while there's one, and ``task_finished`` for every task that ended. While a set
holds merges, a task's verified work is committed in the coder's tree and waits there, unmerged
and unreviewed, but a fix for a set held, which merges with the work stacked beneath it; once no
hold is left, the work waiting lands, in order, and is told of as landed then; the work waiting is
saved with the session, at each change, and taken on again as the session opens. A blocking
finding's fix merges despite the holds too. A chain of
fixes for a set held that ends without merging is told to the holds, which queue the next. A run
stops
at a task its agent couldn't do for the usage limit or for the API not answering, as the
launcher of its role's runtime tells its error, at a fix that ends in error, which must run
before anything puts its work aside, once the session halts, and before a task its caller holds
back.
"""

import asyncio
from collections.abc import Callable, Collection, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, field
from functools import partial
from typing import Literal

from orbit.agent.application.agent_run import AgentRun
from orbit.agent.application.coder_slots import CoderSeat
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.landing import Lander, Landing, Stacked
from orbit.agent.application.lane_runner import runtime_stop
from orbit.agent.application.lanes import OPEN_GATE, Background, SessionGate, StoppableRun
from orbit.agent.application.merge_holds import MergeHolds
from orbit.agent.application.outcomes import Outcome, UserStop, ended, run_task_to_end
from orbit.agent.application.record_moves import RecordMoves
from orbit.agent.application.review_lane import ReviewLane
from orbit.agent.application.role_workspace import workspace_for
from orbit.agent.application.set_runs import SetRuns
from orbit.agent.application.startup_check import StartupCheck
from orbit.agent.application.task_trace import TaskTracing
from orbit.agent.application.tasks import Changed, Tasks, unchanged
from orbit.agent.application.triggers import TriggerEngine
from orbit.agent.application.verifier import Failure, Verifier
from orbit.agent.domain.board import CUT_SHORT, TaskBoard
from orbit.agent.domain.checks import (
    ChecksProposal,
    merge_proposed_sets,
    merge_proposed_triggers,
)
from orbit.agent.domain.commits import commit_message, configure_message
from orbit.agent.domain.endings import (
    STOPPED_BY_YOU,
    WORK_GONE,
    gave_up,
)
from orbit.agent.domain.merge_holds import holding_sets
from orbit.agent.domain.roles import Workspace
from orbit.agent.domain.sessions import SavedWaiting
from orbit.agent.domain.setup_first import waiting_for_setup
from orbit.agent.domain.usage_limits import FIX_ERROR_NOTE, Outages, RuntimeStop, clears_outage
from orbit.agent.domain.verification import (
    CheckSet,
    FallbackFixes,
    failure_reason,
    fix_prompt,
)
from orbit.execution.cancelling import stop, waited_out
from orbit.protocol import (
    CHECKS_EXHAUSTED,
    ErrorOccurred,
    Notice,
    TaskMerged,
    TaskSpec,
    TaskStarted,
    TaskState,
)
from orbit.tracing import Span, current_span

RunEnd = Literal["finished", "cancelled", "limited", "api_unavailable", "fix_failed"]
"""Why a run ended: no task was left to run, one was cancelled, an agent hit its runtime's usage
limit or found its runtime's API not answering, or a fix ended in error."""

Seating = Literal["seated", "unneeded", "dropped"]
"""How seating a task for a coder's slot ended: it may run, the board's next task stopped
needing a slot, or the run's place in line was dropped from outside the lane, as when the user
stops the run or its workspace closes or switches sessions."""


@dataclass(frozen=True)
class Beside:
    """What runs beside the work lane: the review lane, if any, and the startup check, if any,
    whose panel each task takes over; the session's ``gate``, which stops a run halted before
    its next task; its seat at the ``coders``' slots, if any, which a task whose role works in
    the worktree takes one of before it starts; the ``tracing`` of its tasks; the ``records``
    moves, if any, which move a record a task names whose number another took before the task
    runs; the roles whose tasks run ``elsewhere``, in lanes that start by themselves; the
    ``disabled`` roles, whose tasks run nowhere; each runtime's ``outages``, shared with the
    lanes that start by themselves, in which a run notes the runtime that stopped it and the
    runtimes whose agents ended their runs without an outage; the ``triggers``, if any,
    which hear how each task ended, and of its landing while there's no review lane to raise
    it; the named ``sets``' runs, if any, whose panel each task takes over too; the ``holds``
    on merges, if any, which keep a task's work waiting in the tree; and ``save``, which saves
    the session as the work waiting changes."""

    review: ReviewLane | None = None
    startup: StartupCheck | None = None
    gate: SessionGate = OPEN_GATE
    coders: CoderSeat | None = None
    tracing: TaskTracing = field(default_factory=TaskTracing)
    records: RecordMoves | None = None
    elsewhere: frozenset[str] = frozenset()
    disabled: frozenset[str] = frozenset()
    outages: Outages = field(default_factory=Outages)
    triggers: TriggerEngine | None = None
    sets: SetRuns | None = None
    holds: MergeHolds | None = None
    save: Changed = unchanged


@dataclass(frozen=True)
class _Waiting:
    """Verified work waiting in the coder's tree for merges to resume: the ``count`` of commits
    of the task that ended as ``state``, and their ``subjects``, oldest first, which find them
    after a rebase; none if they aren't known."""

    state: TaskState
    count: int
    subjects: tuple[str, ...]

    @property
    def stacked(self) -> Stacked:
        """The work as a landing stacks it."""
        return self.state.id, self.subjects


class WorkLane:
    """Runs ``tasks``' tasks through ``desk``'s agents, checked by ``verifier`` if any.

    Reviews are left to ``beside``'s review lane, when there's one, the tasks of the roles it
    says run elsewhere to their own lanes, and the tasks of the roles it says are disabled on
    the board, unrun. :meth:`run` is meant to run as
    a task of its own: cancelling it cancels the task running, and the cancel goes on once that
    task has ended. The user's stop, :meth:`stopped_by_user`, cancels only the agent's run, and
    the run ends by itself: work the agent finished before the stop is still settled, and
    lands. A task whose role works in the worktree takes a slot at ``beside``'s coders, waiting
    in line for one, before it starts, and gives it back once it has ended, however it does; the
    user's stop leaves the line. Each task is traced, from before it
    waits for a slot until its status is final, with ``beside``'s tracing, linked to the span
    current as its run started: what started the run.
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
        self._records = beside.records
        self._elsewhere = beside.elsewhere
        self._disabled = beside.disabled
        self._outages = beside.outages
        self._triggers = beside.triggers
        self._sets = beside.sets
        self._holds = beside.holds
        self._save = beside.save
        self._waiting: list[_Waiting] = []  # The work waiting in the tree, in the order it lands.
        # The work that landed stacked beneath a task's, each with its last commit, if still
        # there, until told of.
        self._stacked: list[tuple[TaskState, str | None]] = []
        self._tree_lock = asyncio.Lock()  # Held by a task working in the tree, or a landing.
        self._releasing = Background("landing", desk.publish)
        self._seated = False  # The run holds a coder's slot, for the task about to run or running.
        # Task id -> the commit it merged into the base, until reported.
        self._landed: dict[str, str] = {}
        # The fixes for sets held whose work merged, running their sets again, until reported.
        self._fixes_landed: set[str] = set()
        self._starts: dict[str, str] = {}  # Task id -> the tree's head when it started.
        # Fixes queued per step along each chain, kept across runs so a restart can't reset
        # the cap.
        self._fallbacks = FallbackFixes()
        self._running = False  # A run is going.
        # The agent's run of the current task, and whether the user stopped the run going now.
        self._agent = StoppableRun[Outcome]()
        self._current: str | None = None  # The task running, until the review lane hears of it.
        # The task a run stopped at, which the next one runs first: a fix must run before
        # another task puts its work aside.
        self._resume: str | None = None
        self._runtime_stop: RuntimeStop | None = None  # The failure that last stopped a run.

    def reset(self, waiting: Sequence[SavedWaiting] = ()) -> None:
        """Forget the fix counts, the task a run stopped at and what its tasks landed and
        started from, of the session left, whose task ids the next one reuses, and take on the
        work the session opened saved ``waiting``, in the order it lands. The session left keeps
        its own work waiting, as it was saved as the switch began, to land once it's open
        again."""
        self._fallbacks = FallbackFixes()
        self._resume = None
        self._landed.clear()
        self._fixes_landed.clear()
        self._starts.clear()
        self._waiting = [_Waiting(each.state, each.count, each.subjects) for each in waiting]
        self._stacked.clear()

    @property
    def waiting_to_merge(self) -> tuple[str, ...]:
        """The ids of the tasks whose verified work waits in the tree for merges to resume, in
        the order it lands."""
        return tuple(waiting.state.id for waiting in self._waiting)

    @property
    def waiting(self) -> tuple[SavedWaiting, ...]:
        """The verified work waiting in the tree for merges to resume, in the order it lands,
        as the session saves it."""
        return tuple(
            SavedWaiting(state=each.state, count=each.count, subjects=each.subjects)
            for each in self._waiting
        )

    def release(self) -> None:
        """Land the work waiting, in the background, as no hold is left, once no task works in
        the tree, unless the session is halted, as lifting the halt does again, or the holds
        kept are unread, as reading them does, as :meth:`_landable` says."""
        if self._waiting and not self._gate.halted():
            self._releasing.start(self._land_waiting())

    async def stop_releasing(self) -> None:
        """Stop landing the work waiting, if it's landing, and wait for it to stop."""
        await self._releasing.stop()

    def stopped_by_user(self) -> bool:
        """Stop the run going now, as the user asked: cancel the agent's run of its task, which
        that task says, and run no other; returns whether a run was going."""
        if not self._running:
            return False
        self._agent.stop()
        if self._coders is not None:
            self._coders.drop()
        return True

    @property
    def current(self) -> str | None:
        """The task the run is on or, before it picks one, the one it will pick; none when
        there's none."""
        return self._current or self.next_work()

    @property
    def runtime_stop(self) -> RuntimeStop | None:
        """The failure, on its runtime, that last stopped a run for a usage limit or the API
        not answering; none before one did."""
        return self._runtime_stop

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
        """The next task for the work lane, the one a run stopped at first; the roles of the
        review lane, if there is one, and of the lanes that start by themselves run there, the
        disabled roles' run nowhere, and while verification is missing, as the gate says, every
        task but the configurator's waits."""
        reviews = self._review.roles if self._review is not None else frozenset()
        exclude = self._elsewhere | reviews | self._disabled
        waiting = waiting_for_setup(self._gate.verification(), self._board.tasks)
        skip = {*tried, *waiting}
        return self._board.next_runnable(skip=skip, exclude=exclude, first=self._resume)

    @property
    def disabled_waiting(self) -> str | None:
        """The role of the first task still to run whose role is disabled, which no run runs;
        none if there's no such task."""
        task_id = self._board.next_runnable(roles=self._disabled)
        return None if task_id is None else self._board.get(task_id).task.role

    async def run(self, held: Callable[[str], bool] = lambda _: False) -> RunEnd:
        """Run the tasks waiting, in order but the one the last run stopped at first, until
        none are left or the next is one ``held`` holds back by its id, left waiting, which ends
        it as finished, the user stops one, the session halts, which ends it as cancelled, or one
        ends in error for its runtime's usage limit or API not answering, noted as the
        :attr:`runtime_stop`, or as a fix; returns which. Any other cancel goes on once the task
        running has ended. Each task's span links to the span current now."""
        self._running = True
        self._agent.reset()
        try:
            return await self._run_tasks(current_span(), held)
        finally:
            self._running = False

    async def _run_tasks(self, cause: Span, held: Callable[[str], bool]) -> RunEnd:
        """Run the tasks waiting, as :meth:`run` does, their spans linked to ``cause``, what
        started the run, up to one ``held`` holds back; returns why the run ended."""
        tried: set[str] = set()
        try:
            while (task_id := self.next_work(tried)) is not None:
                if self._gate.halted():  # It may have halted as the last task ended.
                    return "cancelled"
                if held(task_id):
                    return "finished"
                model = self._model(task_id)
                with self._tracing.task(self._board, task_id, model, (cause,)):
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
        """Seat task ``task_id`` and run it, as :meth:`_run_seated` does; the run goes on,
        picking its next task again, if the board stopped needing a slot as it waited for one,
        unless the user stopped it, and ends cancelled if its place in line was otherwise
        dropped. A slot is given back once a task has run, and kept for the next task if this
        one didn't."""
        seating = await self._seat(task_id, tried)
        if seating != "seated":
            return None if seating == "unneeded" and not self._agent.stopped else "cancelled"
        try:
            return await self._run_seated(task_id, tried)
        finally:
            if task_id in tried:
                self._unseat()

    async def _seat(self, task_id: str, tried: set[str]) -> Seating:
        """Take a coder's slot for task ``task_id``, waiting in line for one as
        :meth:`_wait_in_line` does, if its role takes one and no slot is held already, or else
        give back any held; returns whether it may run or, if not, why not."""
        coders = self._coders
        if coders is None or not self._takes_slot(task_id):
            self._unseat()
            return "seated"
        if self._seated:
            return "seated"
        return await self._wait_in_line(coders, tried)

    async def _wait_in_line(self, coders: CoderSeat, tried: set[str]) -> Seating:
        """Take a slot at ``coders``, waiting in line for one; returns whether it took one or,
        if its place in line was dropped, who dropped it: the watch on the board, as the
        board's next task, among those not ``tried``, no longer takes a slot, or something
        outside the lane. The watch has ended, however this ends; its failure, a bug,
        escapes."""
        watch = asyncio.create_task(self._leave_line_unneeded(coders, tried))
        try:
            self._seated = await self._tracing.seat(coders)
        finally:
            await stop(watch)
        if self._seated:
            return "seated"
        # A watch still waiting when stopped didn't drop the place.
        return "dropped" if watch.cancelled() else "unneeded"

    async def _leave_line_unneeded(self, coders: CoderSeat, tried: set[str]) -> None:
        """Drop the run's place in line at ``coders`` once the board's next task, among those
        not ``tried``, takes no slot."""
        while self._takes_slot(self.next_work(tried)):
            await self._tasks.moved()
        coders.drop()

    def _takes_slot(self, task_id: str | None) -> bool:
        """Whether task ``task_id``, if any, is of a role that takes a coder's slot, as the
        session's roles say; one of a role it doesn't have takes none, failing without an
        agent."""
        if task_id is None:
            return False
        role = self._desk.agents.roles.get(self._board.get(task_id).task.role)
        return role is not None and role.takes_coder_slot

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
        started = self._board.get(task_id)
        outcome = await self._run_task(task_id)
        return await self._end_at(started, outcome)

    async def _end_at(self, started: TaskState, outcome: Outcome) -> RunEnd | None:
        """Why the run ends at the task that was ``started``, as it was then, which ended in
        ``outcome``: it was cancelled or stopped, or the run stops at its error, to run it first
        next time; none if the run goes on."""
        if outcome.status == "cancelled" or self._agent.stopped:
            return "cancelled"
        if outcome.status == "error" and (end := await self._stop_at(started, outcome)):
            self._resume = started.id
            return end
        return None

    async def _stop_at(self, started: TaskState, error: Outcome) -> RunEnd | None:
        """Why the run stops at the task that was ``started``, as it was then, which ended in
        ``error``: its agent hit the usage limit or found the API not answering, as its role's
        launcher tells, as :meth:`_stop_for` says, or it was fixing another, said so; none if
        the run goes on."""
        stop = self._failure(started.task.role, error)
        if stop is not None:
            return self._stop_for(stop, error.detail)
        if started.fixes is None:
            return None
        await self._publish(Notice(message=FIX_ERROR_NOTE))
        return "fix_failed"

    def _stop_for(self, stop: RuntimeStop, error: str) -> RunEnd:
        """Why the run stops for ``stop``, noted as the :attr:`runtime_stop` and, with
        ``error``, among the outages: the usage limit, or the API not answering."""
        self._runtime_stop = stop
        self._outages.stopped(stop, error)
        return "limited" if stop.failure.kind == "usage_limit" else "api_unavailable"

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
        run = partial(self._run_started, task_id)
        stop = UserStop(STOPPED_BY_YOU, self._cancelled)
        state = self._board.get(task_id)  # Taken now: the task may be removed once it finishes.
        outcome: Outcome | None = None
        try:
            outcome = await run_task_to_end(self._tasks, task_id, run, stop, self._publish)
            return outcome
        finally:
            await self._report_to_review(state, outcome)

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
        if self._sets is not None:
            self._sets.release_panel()
        if self._verifier is not None:
            await self._verifier.reset()
        await self._publish(TaskStarted(task_id=task_id))

    async def _report_to_review(self, state: TaskState, outcome: Outcome | None) -> None:
        """Tell the review lane the commit the task that was ``state`` merged, if any, or else,
        if it finished in ``outcome`` rather than being cut short, that it ended, the triggers
        that it finished, if it did, and the holds, if it's a fix that didn't land. The task is
        told of as it ended, without looking it up: a task is published as finished before the
        save that follows, and one removed in that window is reviewed all the same."""
        await self._report_stacked()
        commit = self._landed.pop(state.id, None)
        fix_landed = state.id in self._fixes_landed
        self._fixes_landed.discard(state.id)
        if outcome is None:  # Cut short: only work that landed is told of, to be reviewed.
            if commit is not None:
                await self._report_landed(state, commit)
            return
        ended = state.model_copy(update={"status": outcome.status})
        await self._report_end(ended, commit, self._failure(state.task.role, outcome))
        if not fix_landed:
            await self._report_unlanded_fix(ended)

    async def _report_unlanded_fix(self, state: TaskState) -> None:
        """Tell the holds, if any, that the task that ended as ``state`` ended without its work
        merging, in case it's a fix for a set held; not if it was cancelled or ended in error,
        either of which leaves it to run again."""
        if self._holds is not None and state.status not in CUT_SHORT:
            await self._holds.hear_unlanded_fix(state)

    async def _report_end(
        self, state: TaskState, commit: str | None, stop: RuntimeStop | None
    ) -> None:
        """Tell the review lane that the task that ended as ``state`` landed ``commit``, if it
        did, or else that it ended, stopped by its runtime's limit or an outage, ``stop``, if
        one did, which leaves it for a run to start with; and the triggers that it finished."""
        if commit is not None:
            await self._report_landed(state, commit)
        elif self._review is not None and state.id not in self.waiting_to_merge:
            await self._review.ended(state, stop)
        if self._triggers is not None:
            self._triggers.run(await self._triggers.finished(state))

    async def _report_landed(self, state: TaskState, commit: str) -> None:
        """Tell the review lane, which raises ``task_landed``, that the task that ended as
        ``state`` landed ``commit``, or the triggers themselves while there's no review lane."""
        if self._review is not None:
            await self._review.landed(state, commit)
        elif self._triggers is not None:
            self._triggers.run(await self._triggers.landed(state, commit))

    async def _run_agent(self, task_id: str) -> Outcome:
        """Run task ``task_id`` through a new agent in its role's workspace and land its work;
        a fallback queues a fix for a fresh one."""
        # A fix carries on from the work it fixes.
        keep = self._board.get(task_id).fixes is not None
        where = self._workspace(task_id)
        guard: AbstractAsyncContextManager[None] = (
            self._tree_lock if where == "worktree" else nullcontext()
        )
        try:
            async with guard, workspace_for(where, self._desk, keep) as root:
                await self._note_start(task_id)
                return await self._run_prepared(task_id, root)
        finally:
            self._starts.pop(task_id, None)

    async def _run_prepared(self, task_id: str, root: str) -> Outcome:
        """Run task ``task_id``, its workspace readied at ``root``, through a new agent and land
        its work, as :meth:`_run_agent` does."""
        if not await self._holds_fixed_work(task_id):
            return ended("gave_up", gave_up(WORK_GONE))
        await self._move_records(task_id, root)
        task = self._board.get(task_id).task
        verifier = self._verifier
        before = await verifier.state_before(task, root) if verifier is not None else None
        with self._tracing.run() as run:
            agent = AgentRun(task_id, self._tasks, verifier, self._desk)
            outcome = await self._agent.run_alone(agent.run(before, root, run))
            self._heard_from(task.role, outcome)
            return await self._settle(task_id, outcome, root)

    def _heard_from(self, role: str, outcome: Outcome) -> None:
        """Forget the outage of ``role``'s runtime, if the session has that role, once an agent
        in it finished its run in ``outcome`` without the API not answering, as its launcher
        tells and :func:`clears_outage` says, however its task ended. The user's stop doesn't
        reach this, as it cancels the agent's run, so it leaves the outage as it is."""
        runtime = self._desk.agents.runtime(role)
        if runtime is not None and clears_outage(self._failure(role, outcome)):
            self._outages.heard_from(runtime)

    def _failure(self, role: str, outcome: Outcome) -> RuntimeStop | None:
        """The failure the launcher of ``role``'s runtime tells in the error a task in it ended
        in, ``outcome``, on that runtime; none if it ended otherwise, the launcher tells none,
        or the session has no such role, whose task fails without an agent."""
        return runtime_stop(self._desk.agents, role, outcome)

    async def _settle(self, task_id: str, outcome: Outcome, root: str) -> Outcome:
        """What task ``task_id``'s run ending in ``outcome`` at ``root`` comes to: the checks
        accepted from it are used, its completed work lands, and a fallback queues a fix. The
        user's stop doesn't reach this, as it cancels only the agent's run."""
        verifier = self._verifier
        if outcome.checks is not None:
            assert verifier is not None  # Checks are only proposed to a verifier.
            # Waited out: a save in a thread goes on when cancelled, so the checks it saves
            # are used too.
            await waited_out(self._configure(verifier, outcome.checks, root))
        if outcome.status == "completed":
            outcome = await self._land(task_id, outcome, root)
        if outcome.failure is not None and outcome.status == "unverified":
            assert verifier is not None  # Only verification fails.
            await self._queue_fix(verifier, task_id, outcome.failure)
        return outcome

    async def _configure(self, verifier: Verifier, checks: ChecksProposal, root: str) -> None:
        """Save the ``checks`` the user accepted into the config at ``root``, and use them from
        the next task on: their task checks and setup, and their sets and triggers, as the config
        reads them over those configured, which the sets' runs and the holds on merges take on
        too. This, or a restart, is the only way the checks change. A failed save raises
        :class:`ConfigWriteFailed` before anything changes, ending the task in error."""
        if self._desk.agents.write_config is not None:
            await asyncio.to_thread(self._desk.agents.write_config, root, checks)
        agents = self._desk.agents
        if agents.tree is not None:
            agents.tree.use_setup(checks.setup)
        triggers = merge_proposed_triggers(agents.triggers, agents.built_in_triggers, checks)
        self._desk.use_triggers(triggers)
        verifier.use_sets(merge_proposed_sets(verifier.verification.sets, checks))
        await self._use_sets(verifier.verification.sets)
        await verifier.use_steps(checks.task_checks)

    async def _use_sets(self, sets: tuple[CheckSet, ...]) -> None:
        """Run the named ``sets`` as triggers name them, and have those that hold merges hold
        them, from now on."""
        if self._sets is not None:
            self._sets.use_sets(sets)
        if self._holds is not None:
            await self._holds.use_sets(holding_sets(sets))

    async def _land(self, task_id: str, outcome: Outcome, root: str) -> Outcome:
        """Commit and merge a completed task's work, when it worked in the coder's tree: the
        checks the user accepted from it, or else the work itself. A rebase is rechecked only
        if the task's role is verified. The work has landed, for review, only once it merged:
        the task stays completed either way, with the merge's detail."""
        tree = self._desk.agents.tree
        if tree is None or self._workspace(task_id) != "worktree":
            return outcome
        state = self._board.get(task_id)
        message = _message(state, outcome)
        if self._held_back(state):
            await self._wait(state, message)
            return outcome
        landing = await self._merge(
            task_id, Lander(tree, self._recheck(state), self._publish), message, root
        )
        if landing.failure is not None:
            reason, detail = failure_reason(*landing.failure, rebased=True)
            return Outcome("unverified", landing.failure, reason=reason, detail=detail)
        self._landed_fix(state, landing)
        return outcome

    def _held_back(self, state: TaskState) -> bool:
        """Whether the work of the task that is ``state`` waits in the tree rather than
        merging: a hold is on, and it's no fix for a hold on, a set's or a blocking
        finding's."""
        holds = self._holds
        return holds is not None and holds.held and not holds.is_fix(state)

    def _landed_fix(self, state: TaskState, landing: Landing) -> None:
        """Have the set the task that is ``state`` fixes, if it's a fix for a set held, run
        again, if ``landing`` its work merged it."""
        holds = self._holds
        if holds is None or landing.result != "merged":
            return
        fixed = holds.fix_of(state)
        if fixed is not None:
            self._fixes_landed.add(state.id)
            holds.fix_landed(fixed, state.origin)

    def _recheck(self, state: TaskState) -> Verifier | None:
        """What rechecks the work of the task that is ``state`` after a rebase: the verifier,
        if its role is verified and it's still on the board, where the verifier finds it."""
        role = self._desk.agents.roles.get(state.task.role)
        verified = role is not None and role.verify and self._board.find(state.id) is not None
        return self._verifier if verified else None

    async def _wait(self, state: TaskState, message: str) -> None:
        """Commit the work of the task that is ``state`` with ``message`` and leave it waiting
        in the tree, as the holds hold merges, if it made any commit, telling them it waits."""
        tree, holds = self._desk.agents.tree, self._holds
        assert tree is not None and holds is not None  # Only work in the tree is held back.
        await tree.commit(message)
        made = await tree.list_commits(self._starts[state.id], await tree.head())
        if made:
            done = state.model_copy(update={"status": "completed"})
            self._waiting.append(_Waiting(done, len(made), tuple(c.subject for c in made)))
            await self._waiting_changed()

    async def _merge(self, task_id: str, lander: Lander, message: str, root: str) -> Landing:
        """What landing task ``task_id``'s work with ``lander``, committed with ``message`` at
        ``root``, with the work waiting stacked beneath it, came to; work that merged is noted
        as landed, however the landing ends."""
        beneath = tuple(self._waiting)
        stacked = [waiting.stacked for waiting in beneath]
        try:
            return await lander.land(task_id, message, root, self._starts[task_id], stacked)
        finally:
            if lander.merged is not None:
                self._landed[task_id] = lander.merged
            self._take_stacked(beneath, lander.stacked)

    def _take_stacked(self, beneath: Sequence[_Waiting], stacked: Sequence[str | None]) -> None:
        """Note the work waiting ``beneath`` a landing as landed with the ``stacked`` commits,
        each piece's last, if the landing merged it, to be told of in order."""
        if not stacked:
            return
        self._stacked += [(w.state, c) for w, c in zip(beneath, stacked, strict=True)]
        self._waiting = [waiting for waiting in self._waiting if waiting not in beneath]

    async def _report_stacked(self) -> None:
        """Tell of each piece of work that landed stacked beneath another, in order: as landed,
        with its last commit, as :meth:`_report_landed` does, or, its commits dropped as a
        rebase left them empty, as ended with nothing landed."""
        stacked, self._stacked = self._stacked, []
        for state, commit in stacked:
            if commit is None:
                await self._report_unlanded(state)
            else:
                await self._report_landed(state, commit)
        if stacked:
            await self._waiting_changed()

    async def _waiting_changed(self) -> None:
        """Tell the holds, if any, that the work waiting changed, and save the session."""
        if self._holds is not None:
            self._holds.changed()
        await self._save()

    async def _land_waiting(self) -> None:
        """Land the work waiting in the tree, in order, once no task works there, unless a
        hold is on again, or the session halted, by then; work that didn't merge is told of as
        ended, with the merge's detail."""
        async with self._tree_lock:
            waiting = tuple(self._waiting)
            if self._landable(waiting):
                landing = await self._land_stacked(waiting)
                await self._report_stacked()
                await self._report_unmerged(waiting, landing)

    def _landable(self, waiting: Sequence[_Waiting]) -> bool:
        """Whether the work ``waiting`` may land now: there's some, the holds on merges are
        known and none is on, and the session isn't halted. Work a session saved waiting lands
        without holds, as when no set holds merges any longer; while the holds kept are unread,
        it waits for them, which tell :meth:`release` once they're read and none is left."""
        holds = self._holds
        held = holds is not None and (holds.held or not holds.is_known)
        return bool(waiting) and not held and not self._gate.halted()

    async def _land_stacked(self, waiting: Sequence[_Waiting]) -> Landing:
        """What landing the work ``waiting`` in the tree came to, rechecked after a rebase as
        the last piece's work; what merged is noted as landed, however the landing ends."""
        tree = self._desk.agents.tree
        assert tree is not None  # Only work in the tree waits.
        last = waiting[-1].state
        lander = Lander(tree, self._recheck(last), self._publish)
        stacked = [each.stacked for each in waiting]
        try:
            return await lander.land_stacked(await tree.prepare(True), stacked)
        finally:
            self._take_stacked(waiting, lander.stacked)

    async def _report_unmerged(self, waiting: Sequence[_Waiting], landing: Landing) -> None:
        """Tell of each piece of work ``waiting`` that ``landing`` didn't merge as ended with
        its detail, nothing landed: it stays in the tree, as work that didn't merge does."""
        if landing.result == "merged":
            return
        self._waiting = [each for each in self._waiting if each not in waiting]
        for each in waiting:
            if landing.result is not None:
                merged = TaskMerged(
                    task_id=each.state.id, result=landing.result, detail=landing.detail
                )
                await self._publish(merged)
            await self._report_unlanded(each.state)
        await self._waiting_changed()

    async def _report_unlanded(self, state: TaskState) -> None:
        """Tell the review lane, if any, that the task that ended as ``state``, whose work
        waited, landed nothing after all; the triggers heard it finish as it ended."""
        if self._review is not None:
            await self._review.ended(state)

    def _workspace(self, task_id: str) -> Workspace:
        """Where task ``task_id``'s agent works, as its role says; the coder's tree for a role
        the session doesn't have, as such a task fails without an agent."""
        role = self._desk.agents.roles.get(self._board.get(task_id).task.role)
        return "worktree" if role is None else role.workspace

    async def _note_start(self, task_id: str) -> None:
        """Note the coder's tree's head as task ``task_id`` starts, if it works there, so its
        landing counts the commits its agent makes itself."""
        tree = self._desk.agents.tree
        if tree is not None and self._workspace(task_id) == "worktree":
            self._starts[task_id] = await tree.head()

    async def _move_records(self, task_id: str, root: str) -> None:
        """Move each record task ``task_id`` names whose number another record at ``root``
        took, when there are record moves."""
        if self._records is not None:
            await self._records.move(task_id, root)

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
        failed = self._board.get(task_id).task
        fix = TaskSpec(
            prompt=fix_prompt(step, output), settings=failed.settings, skills=failed.skills
        )

        def add(board: TaskBoard) -> None:
            fix_id = board.add(fix, fixes=task_id)
            self._fallbacks.record(fix_id, task_id, step.name)
            if self._review is not None:
                self._review.joined(fix_id)

        await self._tasks.change(add)


def _message(state: TaskState, outcome: Outcome) -> str:
    """The message the work of the task that is ``state``, which ended in ``outcome``, is
    committed with: the checks the user accepted from it, or else the work itself."""
    if outcome.checks is not None:
        return configure_message(outcome.checks)
    return commit_message(state.task, outcome.summaries, fix=state.fixes is not None)
