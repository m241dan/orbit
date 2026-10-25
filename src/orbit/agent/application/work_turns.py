"""When the work lane runs: on the user's asking, after a review queues work unless the user
held it or the session halted, first for a configurator, and again by itself once Claude's
usage limit that stopped it resets, or, if Claude's API not answering stopped it, after a backoff
that grows with each outage in a row, saying meanwhile until when and why it's paused. A run
remembers what started it, which its tasks' spans link to."""

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
    OUTAGE_RESUMING_NOTE,
    RESUMING_NOTE,
    WAIT_CANCELLED_NOTE,
    Outage,
    Paused,
    outage_delay,
    outage_paused_note,
    paused_note,
    resume_time,
)
from orbit.protocol import (
    CONFIGURATOR,
    NOT_CONFIGURED,
    PAUSED_FOR_API_UNAVAILABLE,
    PAUSED_FOR_USAGE_LIMIT,
    WORK_LANE,
    Event,
    Notice,
    TaskSpec,
    UsageChanged,
)
from orbit.tracing import Span, current_span, use_span

NO_AGENTS_MESSAGE = "Tasks need agents, and none are set up."
NO_VERIFY_MESSAGE = "Setting up checks needs verification on; orbit was started with --no-verify."
CONFIGURING_NOTE = "A configurator task is already queued or running."
TASKS_RUNNING_MESSAGE = "Tasks are already running."


@dataclass(frozen=True)
class WorkParts:
    """What decides when the work lane runs: the ``work`` lane and review ``lane``, if any, the
    ``tasks`` they run, the ``verifier`` and agents' ``desk``, if any, ``post``, which
    publishes events without blocking, the session's ``gate``, which tells whether it's
    halted, ``usage``, the usage limits fetched last, if any, and ``paused_changed``, called
    whenever :attr:`WorkTurns.paused` changes."""

    work: WorkLane | None
    lane: ReviewLane | None
    tasks: Tasks
    verifier: Verifier | None
    desk: AgentDesk | None
    post: Callable[[Event], None]
    gate: SessionGate
    usage: Callable[[], UsageChanged | None] = lambda: None
    paused_changed: Callable[[], None] = lambda: None


@dataclass(frozen=True)
class _Restart:
    """A stopped run starting again by itself: the span that started it, ``cause``, and the
    ``note`` said as it starts again."""

    cause: Span
    note: str


class WorkTurns:
    """Runs the work lane's turns from ``parts``, one at a time.

    The user's :meth:`run_tasks` starts a run, refused while one runs; :meth:`hold` cancels it
    and keeps :meth:`wake`, a review's request, from starting another until the user asks
    again. While the session is halted, :meth:`wake` starts nothing either. A run Claude's
    usage limit stopped starts again by itself once the limit resets, and one Claude's API not
    answering stopped after a backoff, longer for each outage in a row, unless the user runs the
    work, sets up checks or holds it first, or :meth:`forget_wait` is called as the session is
    left; a halt's :meth:`stop_turn` keeps the wait, and one ending while halted starts the run
    once :meth:`lifted` is called. Until the run starts again or its wait is cancelled or
    dropped, :attr:`paused` says when it starts again and why. Once :meth:`close` is called,
    nothing starts.

    A run is started by the span current as it's asked for: the command's asking for it, or the
    review's whose suggestions it runs. The run keeps that span when it starts again by itself,
    and its tasks' spans link to it.
    """

    def __init__(self, parts: WorkParts) -> None:
        self._parts = parts
        self._board = parts.tasks.board
        self._held = False  # The user cancelled: don't start work by itself.
        self._closed = False  # The host is going away: start nothing.
        self._turns = TurnSlot(WORK_LANE, TASKS_RUNNING_MESSAGE, parts.post)
        # A run the usage limit or an outage stopped waits here to start again.
        self._wait = Background(WORK_LANE, _posting(parts.post))
        self._waiting = False  # The wait hasn't ended yet.
        # How the run whose wait ended while the session was halted starts again, if one did.
        self._due: _Restart | None = None
        self._paused: Paused | None = None  # Until the waiting run starts again, and why.

    @property
    def running(self) -> bool:
        return self._turns.running

    @property
    def paused(self) -> Paused | None:
        """When the run the usage limit or an outage stopped starts again, and why, until it
        does or its wait is cancelled or dropped; none while no run waits to."""
        return self._paused

    async def run_tasks(self) -> None:
        """Start a run for the user, lifting a hold; refused without agents, or while one
        runs."""
        self.check_run_tasks()
        await self._start_asked()

    async def configure_checks(self) -> None:
        """Queue a configurator first and run it, in place of a wait to start again, unless work
        is running already (then it goes next); refused without verification or while one is
        queued or running."""
        self.check_configure_checks()
        desk, verifier = self._parts.desk, self._parts.verifier
        assert desk is not None and verifier is not None  # Checked just now.
        if self._board.has_waiting(CONFIGURATOR):
            self._parts.post(Notice(message=CONFIGURING_NOTE))
            return
        task = _configurator_task(desk.agents, verifier)
        await self._parts.tasks.change(lambda board: board.add_first(task))
        if not self.running:
            await self._start_asked()

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
        """Run the work a review queued, started by the review's span, the current one, unless
        something runs, the user cancelled or the session is halted."""
        await self._start(current_span(), asked=False)

    def forget_wait(self) -> bool:
        """Cancel the wait to start again of a run the usage limit or an outage stopped, or its
        start once a halt lifts; returns whether there was one."""
        waiting, self._waiting = self._waiting, False
        due, self._due = self._due is not None, None
        self._wait.cancel()
        self._pause_as(None)
        return due or waiting

    async def stop_turn(self) -> None:
        """Cancel the run, if any, as the session halts, and wait for it to end, keeping its
        wait to start again."""
        await self._turns.close()

    async def lifted(self) -> None:
        """Start the run again whose wait ended while the session was halted, now the halt is
        lifted."""
        restart, self._due = self._due, None
        if restart is not None:
            await self._start_now(restart)

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

    async def _start_asked(self) -> None:
        """Start a run the user asked for, started by the span current, the command's: drop the
        wait to start again of a run the usage limit or an outage stopped, which this run
        replaces, run the reviews waiting, and start the work lane's turn, lifting a hold."""
        self.forget_wait()
        if self._parts.lane is not None:
            self._parts.lane.kick()
        await self._start(current_span(), asked=True)

    async def _start(self, cause: Span, *, asked: bool) -> None:
        """Start the work lane's turn, started by ``cause``, if there is work, unless closed.
        The user ``asked`` lifts a hold, and a halt or a run already running refuses them;
        otherwise a hold, a running run or a halt keeps it waiting."""
        work = self._startable(asked=asked)
        if work is not None:
            await self._turns.start(self._run(work, cause))

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

    async def _run(self, work: WorkLane, cause: Span) -> None:
        """Run ``work``'s tasks, their spans linked to ``cause``, what started the run, then, if
        the usage limit or Claude's API not answering stopped them, wait to start again."""
        with use_span(cause):
            end = await work.run()
        if end == "limited":
            self._wait_for_reset(cause)
        elif end == "api_unavailable":
            assert work.outage is not None  # The outage stopped the run.
            self._wait_out(work.outage, cause)

    def _wait_for_reset(self, cause: Span) -> None:
        """Say when the run the usage limit stopped, started by ``cause``, starts again, and
        start it then."""
        pause = self._pause
        at = resume_time(self._parts.usage(), pause.now(), pause.waits)
        paused = Paused(at, PAUSED_FOR_USAGE_LIMIT)
        self._pause_until(paused, paused_note(at), _Restart(cause, RESUMING_NOTE))

    def _wait_out(self, outage: Outage, cause: Span) -> None:
        """Say when the run ``outage`` stopped, started by ``cause``, starts again, after the
        backoff at its level, and start it then."""
        at = self._pause.now() + outage_delay(outage.level)
        note = outage_paused_note(at, outage.error)
        paused = Paused(at, PAUSED_FOR_API_UNAVAILABLE)
        self._pause_until(paused, note, _Restart(cause, OUTAGE_RESUMING_NOTE))

    def _pause_until(self, paused: Paused, note: str, restart: _Restart) -> None:
        """Say ``note`` about the run that just stopped, and start it again as ``restart`` says
        once it's ``paused`` no longer."""
        self._parts.post(Notice(message=note))
        stopped = self._turns.turn
        assert stopped is not None  # This runs as the turn stopped.
        self._waiting = True
        self._pause_as(paused)
        self._wait.start(self._start_again(stopped, paused.until, restart))

    def _pause_as(self, paused: Paused | None) -> None:
        """Note the run waiting to start again is ``paused`` so, or none is, telling of it if
        that changed."""
        changed = paused != self._paused
        self._paused = paused
        if changed:
            self._parts.paused_changed()

    async def _start_again(
        self, stopped: asyncio.Task[object], at: datetime, restart: _Restart
    ) -> None:
        """Once the ``stopped`` turn has ended, wait until ``at``, then start the run again as
        ``restart`` says, or once the halt lifts if the session is halted then."""
        try:
            await asyncio.wait([stopped])
            pause = self._pause
            await pause.sleep(max(0.0, (at - pause.now()).total_seconds()))
        except Exception:  # Failed, it no longer keeps work waiting; reported as it goes on.
            self._waiting = False
            self._pause_as(None)
            raise
        self._waiting = False
        if self._parts.gate.halted():
            self._due = restart
            return
        await self._start_now(restart)

    async def _start_now(self, restart: _Restart) -> None:
        """Start the run the usage limit or an outage stopped again, as ``restart`` says, if
        nothing keeps it waiting and work is left, and say so; either way it's paused no
        longer."""
        self._pause_as(None)
        work = self._startable(asked=False)
        if work is None:
            return
        self._parts.post(Notice(message=restart.note))
        await self._turns.start(self._run(work, restart.cause))

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
