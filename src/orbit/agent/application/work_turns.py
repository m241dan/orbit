"""When the work lane runs: on the user's asking, after a review queues work unless the user
held it or the session halted, first for a configurator, and again by itself once Claude's
usage limit that stopped it resets."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from orbit.agent.application.desk import AgentDesk, Agents
from orbit.agent.application.lanes import Background, SessionGate, TurnSlot
from orbit.agent.application.limit_pause import LimitPause
from orbit.agent.application.review_lane import ReviewLane
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.verifier import Verifier
from orbit.agent.application.work_lane import WorkLane
from orbit.agent.domain.checks import configure_prompt
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.roles import role_task
from orbit.agent.domain.usage_limits import (
    RESUMING_NOTE,
    WAIT_CANCELLED_NOTE,
    paused_note,
    resume_time,
)
from orbit.protocol import (
    CONFIGURATOR,
    NOT_CONFIGURED,
    WORK_LANE,
    Event,
    Notice,
    TaskSpec,
    UsageChanged,
)

NO_AGENTS_MESSAGE = "Tasks need agents, and none are set up."
NO_VERIFY_MESSAGE = "Setting up checks needs verification on; orbit was started with --no-verify."
CONFIGURING_NOTE = "A configurator task is already queued or running."
TASKS_RUNNING_MESSAGE = "Tasks are already running."


@dataclass(frozen=True)
class WorkParts:
    """What decides when the work lane runs: the ``work`` lane and review ``lane``, if any, the
    ``tasks`` they run, the ``verifier`` and agents' ``desk``, if any, ``post``, which
    publishes events without blocking, the session's ``gate``, which tells whether it's
    halted, and ``usage``, the usage limits fetched last, if any."""

    work: WorkLane | None
    lane: ReviewLane | None
    tasks: Tasks
    verifier: Verifier | None
    desk: AgentDesk | None
    post: Callable[[Event], None]
    gate: SessionGate
    usage: Callable[[], UsageChanged | None] = lambda: None


class WorkTurns:
    """Runs the work lane's turns from ``parts``, one at a time.

    The user's :meth:`run_tasks` starts a run, refused while one runs; :meth:`hold` cancels it
    and keeps :meth:`wake`, a review's request, from starting another until the user asks
    again. While the session is halted, :meth:`wake` starts nothing either. A run Claude's
    usage limit stopped starts again by itself once the limit resets, unless the user runs or
    holds the work first, or :meth:`forget_wait` is called as the session is left; a halt's
    :meth:`stop_turn` keeps the wait, and one ending while halted starts the run once
    :meth:`lifted` is called. Once :meth:`close` is called, nothing starts.
    """

    def __init__(self, parts: WorkParts) -> None:
        self._parts = parts
        self._board = parts.tasks.board
        self._held = False  # The user cancelled: don't start work by itself.
        self._closed = False  # The host is going away: start nothing.
        self._turns = TurnSlot(WORK_LANE, TASKS_RUNNING_MESSAGE, parts.post)
        # A run the usage limit stopped waits here to start again.
        self._wait = Background(WORK_LANE, _posting(parts.post))
        self._waiting = False  # The wait hasn't ended yet.
        self._due = False  # Its wait ended while the session was halted.

    @property
    def running(self) -> bool:
        return self._turns.running

    async def run_tasks(self) -> None:
        """Start a run for the user, lifting a hold; refused without agents, or while one
        runs."""
        self.check_run_tasks()
        self.forget_wait()
        if self._parts.lane is not None:
            self._parts.lane.kick()
        await self._start(asked=True)

    async def configure_checks(self) -> None:
        """Queue a configurator first and run it, unless work is running already (then it goes
        next); refused without verification or while one is queued or running."""
        self.check_configure_checks()
        desk, verifier = self._parts.desk, self._parts.verifier
        assert desk is not None and verifier is not None  # Checked just now.
        if self._board.has_waiting(CONFIGURATOR):
            self._parts.post(Notice(message=CONFIGURING_NOTE))
            return
        task = _configurator_task(desk.agents, verifier)
        await self._parts.tasks.change(lambda board: board.add_first(task))
        if not self.running:
            await self._start(asked=True)

    def check_run_tasks(self) -> None:
        """Refuse :meth:`run_tasks` if it couldn't start a run at all: without agents."""
        if self._parts.work is None:
            raise Refused(NOT_CONFIGURED, NO_AGENTS_MESSAGE)

    def check_configure_checks(self) -> None:
        """Refuse :meth:`configure_checks` if it couldn't run at all: without agents or
        verification."""
        if self._parts.desk is None or self._parts.work is None:
            raise Refused(NOT_CONFIGURED, NO_AGENTS_MESSAGE)
        if self._parts.verifier is None:
            raise Refused(NOT_CONFIGURED, NO_VERIFY_MESSAGE)

    def hold(self) -> None:
        """Cancel the run, or its wait to start again, and hold the lane until the user asks
        for work again."""
        self._held = True
        if self.forget_wait():
            self._parts.post(Notice(message=WAIT_CANCELLED_NOTE))
        if self._turns.running:
            assert self._parts.work is not None  # Only the work lane takes work turns.
            if not self._parts.work.stopped_by_user():
                self._turns.cancel()  # The turn hasn't begun its run, so nothing is left to end.

    async def wake(self) -> None:
        """Run the work a review queued, unless something runs, the user cancelled or the
        session is halted."""
        await self._start(asked=False)

    def forget_wait(self) -> bool:
        """Cancel the wait to start again of a run the usage limit stopped, or its start once a
        halt lifts; returns whether there was one."""
        waiting, self._waiting = self._waiting, False
        due, self._due = self._due, False
        self._wait.cancel()
        return due or waiting

    async def stop_turn(self) -> None:
        """Cancel the run, if any, as the session halts, and wait for it to end, keeping its
        wait to start again."""
        await self._turns.close()

    async def lifted(self) -> None:
        """Start the run again whose wait ended while the session was halted, now the halt is
        lifted."""
        if self._due:
            self._due = False
            await self._start_now()

    async def close(self) -> None:
        """Cancel the run, if any, or its wait to start again, wait for it to end, and start
        nothing from then on."""
        self._closed = True
        self.forget_wait()
        try:
            await self._wait.close()
        finally:  # Cancelled meanwhile, the run is still stopped.
            await self._turns.close()

    @property
    def _waits(self) -> bool:
        """Whether work not asked for waits: held, already running, waiting to start again or
        halted."""
        return self._held or self.running or self._waiting or self._parts.gate.halted()

    async def _start(self, *, asked: bool) -> None:
        """Start the work lane's turn if there is work, unless closed. The user ``asked`` lifts a
        hold, and a halt or a run already running refuses them; otherwise a hold, a running run
        or a halt keeps it waiting."""
        work = self._startable(asked=asked)
        if work is not None:
            await self._turns.start(self._run(work))

    def _startable(self, *, asked: bool) -> WorkLane | None:
        """The work lane, if :meth:`_start` would start its turn now; the user ``asked`` lifts a
        hold, and is refused while the session is halted."""
        work = self._parts.work
        assert work is not None  # Work needs agents.
        if self._closed:
            return None
        if asked:
            # The session may have halted since the command was admitted, as it was saved.
            self._parts.gate.check()
            self._held = False
        elif self._waits:
            return None
        return work if work.next_work() is not None else None

    async def _run(self, work: WorkLane) -> None:
        """Run ``work``'s tasks, then, if the usage limit stopped them, wait to start again."""
        if await work.run() == "limited":
            self._wait_for_reset()

    def _wait_for_reset(self) -> None:
        """Say when the run the usage limit stopped starts again, and start it then."""
        pause = self._pause
        at = resume_time(self._parts.usage(), pause.now(), pause.waits)
        self._parts.post(Notice(message=paused_note(at)))
        stopped = self._turns.turn
        assert stopped is not None  # This runs as the turn stopped.
        self._waiting = True
        self._wait.start(self._start_again(stopped, at))

    async def _start_again(self, stopped: "asyncio.Task[object]", at: datetime) -> None:
        """Once the ``stopped`` turn has ended, wait until ``at``, then start the run again, or
        once the halt lifts if the session is halted then."""
        try:
            await asyncio.wait([stopped])
            pause = self._pause
            await pause.sleep(max(0.0, (at - pause.now()).total_seconds()))
        except Exception:  # Failed, it no longer keeps work waiting; reported as it goes on.
            self._waiting = False
            raise
        self._waiting = False
        if self._parts.gate.halted():
            self._due = True
            return
        await self._start_now()

    async def _start_now(self) -> None:
        """Start the run the usage limit stopped again, if nothing keeps it waiting and work is
        left, and say so."""
        work = self._startable(asked=False)
        if work is None:
            return
        self._parts.post(Notice(message=RESUMING_NOTE))
        await self._turns.start(self._run(work))

    @property
    def _pause(self) -> LimitPause:
        desk = self._parts.desk
        assert desk is not None  # The work lane runs agents.
        return desk.agents.pause


def _posting(post: Callable[[Event], None]) -> Callable[[Event], Awaitable[None]]:
    """Publishing that ``post``s each event."""

    async def publish(event: Event) -> None:
        post(event)

    return publish


def _configurator_task(agents: Agents, verifier: Verifier) -> TaskSpec:
    """A configurator's task, told the steps and setup configured now."""
    steps = verifier.verification.steps
    prompt = configure_prompt(steps, agents.tree.setup if agents.tree else None)
    return role_task(CONFIGURATOR, agents.roles[CONFIGURATOR], prompt)
