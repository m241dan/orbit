"""When the work lane runs: on the user's asking, after a review queues work unless the user
held it or the session halted, first for a configurator, and again by itself once the usage limit
of the runtime that stopped it resets, or, if that runtime's API not answering stopped it, after a
backoff that grows with each outage in a row, saying meanwhile until when, why and on which
runtime it's paused. The lane keeps its order, so it waits; the lanes that start by themselves
start no task on that runtime meanwhile, and run those with it. A run remembers what started it,
which its tasks' spans link to.

A task in a lane that starts by itself whose agent hits its runtime's usage limit or finds its API
not answering pauses that runtime too, timed by the same rules, with a backoff of its own for each
runtime; while one pause is in force, it stays, and the work lane's own stop takes its place."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from orbit.agent.application.desk import AgentDesk, Agents
from orbit.agent.application.lane_runner import Kickable
from orbit.agent.application.lanes import Background, SessionGate, TurnSlot
from orbit.agent.application.limit_pause import LimitPause
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.verifier import Verifier
from orbit.agent.application.work_lane import WorkLane
from orbit.agent.domain.checks import configure_prompt
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.roles import disabled_note, role_task
from orbit.agent.domain.usage_limits import (
    RESUMING_NOTE,
    WAIT_CANCELLED_NOTE,
    Outage,
    Paused,
    RuntimeStop,
    limit_resume_time,
    outage_after,
    outage_paused_note,
    outage_resume_time,
    outage_resuming_note,
    paused_note,
    tasks_resuming_note,
)
from orbit.protocol import (
    CONFIGURATOR,
    NOT_CONFIGURED,
    PAUSED_FOR_API_UNAVAILABLE,
    PAUSED_FOR_USAGE_LIMIT,
    WORK_LANE,
    BusyLane,
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
    """What decides when the work lane runs: the ``work`` lane and the ``lanes`` that start by
    themselves, if any, the ``tasks`` they run, the ``verifier`` and agents' ``desk``, if any,
    ``post``, which publishes events without blocking, the session's ``gate``, which tells
    whether it's halted, ``usage``, the usage limits fetched last, if any, ``paused_changed``,
    called whenever :attr:`WorkTurns.paused` changes, and the work lane's ``name``, the lane
    whose roles merge."""

    work: WorkLane | None
    lanes: Kickable | None
    tasks: Tasks
    verifier: Verifier | None
    desk: AgentDesk | None
    post: Callable[[Event], None]
    gate: SessionGate
    usage: Callable[[], UsageChanged | None] = lambda: None
    paused_changed: Callable[[], None] = lambda: None
    name: BusyLane = WORK_LANE


@dataclass(frozen=True)
class _Restart:
    """A stopped run starting again by itself: the span that started it, ``cause``, and the
    ``note`` said as it starts again; with ``work`` false, a pause a lane that starts by itself
    started, which ends by running the lanes' tasks waiting alone."""

    cause: Span
    note: str
    work: bool = True


class WorkTurns:
    """Runs the work lane's turns from ``parts``, one at a time.

    The user's :meth:`run_tasks` starts a run, refused while one runs; :meth:`hold` cancels it
    and keeps :meth:`wake`, a review's request, from starting another until the user asks
    again. While the session is halted, :meth:`wake` starts nothing either. A run a runtime's
    usage limit stopped starts again by itself once the limit resets, and one a runtime's API
    not answering stopped after a backoff, longer for each outage in a row, unless the user runs
    the work, sets up checks or holds it first, or :meth:`forget_wait` is called as the session
    is left; a halt's :meth:`stop_turn` keeps the wait, and one ending while halted starts the
    run once :meth:`lifted` is called. Until the run starts again or its wait is cancelled or
    dropped, :attr:`paused` says when it starts again, why and on which runtime. The lanes that
    start by themselves are kicked as the run starts again by itself or the user cancels its
    wait, and with every run the user asks for. Once :meth:`close` is called, nothing starts.

    A run is started by the span current as it's asked for: the command's asking for it, or the
    review's whose suggestions it runs. The run keeps that span when it starts again by itself,
    and its tasks' spans link to it.
    """

    def __init__(self, parts: WorkParts) -> None:
        self._parts = parts
        self._board = parts.tasks.board
        self._held = False  # The user cancelled: don't start work by itself.
        self._closed = False  # The host is going away: start nothing.
        self._turns = TurnSlot(parts.name, TASKS_RUNNING_MESSAGE, parts.post)
        # A run the usage limit or an outage stopped waits here to start again.
        self._wait = Background(parts.name, _posting(parts.post))
        self._waiting = False  # The wait hasn't ended yet.
        # How the run whose wait ended while the session was halted starts again, if one did.
        self._due: _Restart | None = None
        self._paused: Paused | None = None  # Until the waiting run starts again, and why.
        # Each runtime's outage, as it last paused the lanes that start by themselves.
        self._outages: dict[str, Outage] = {}

    @property
    def running(self) -> bool:
        return self._turns.running

    @property
    def paused(self) -> Paused | None:
        """When the run the usage limit or an outage stopped starts again, why and on which
        runtime, until it does or its wait is cancelled or dropped; none while no run waits
        to."""
        return self._paused

    async def run_tasks(self) -> None:
        """Start a run for the user, lifting a hold; refused without agents, with a task still
        to run whose role is disabled, or while one runs."""
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
        """Refuse :meth:`run_tasks` if it couldn't start a run at all, without agents, or if a
        task still to run is in a disabled role, which no run runs, naming that role."""
        work = self._parts.work
        if work is None:
            raise Refused(NOT_CONFIGURED, NO_AGENTS_MESSAGE)
        disabled = work.disabled_waiting
        if disabled is not None:
            raise Refused(NOT_CONFIGURED, disabled_note(disabled))

    def check_configure_checks(self) -> None:
        """Refuse :meth:`configure_checks` if it couldn't run at all: without agents or
        verification."""
        if self._parts.desk is None or self._parts.work is None:
            raise Refused(NOT_CONFIGURED, NO_AGENTS_MESSAGE)
        if self._parts.verifier is None:
            raise Refused(NOT_CONFIGURED, NO_VERIFY_MESSAGE)

    def hold(self) -> None:
        """Cancel the run, or its wait to start again, which lets the other lanes start their
        tasks again, and hold the lane until the user asks for work again."""
        self._held = True
        if self.forget_wait():
            self._parts.post(Notice(message=WAIT_CANCELLED_NOTE))
            self._kick_lanes()
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

    def pause_for(self, stop: RuntimeStop, error: str) -> bool:
        """Pause ``stop``'s runtime, whose agent in a lane that starts by itself hit its usage
        limit or, with ``error``, found its API not answering, timed as the work lane's pauses
        are, then run the lanes' tasks waiting; returns whether that runtime is paused now. A
        pause already in force stays, so another runtime isn't paused, and nothing is once
        closed."""
        if self._paused is not None or self._closed:
            return self._paused is not None and self._paused.runtime == stop.runtime
        paused, note, resuming = self._lane_pause(stop, error)
        self._wait_until(paused, note, _Restart(current_span(), resuming, work=False), None)
        return True

    def heard_from(self, runtime: str) -> None:
        """Forget ``runtime``'s outage, as it paused the lanes that start by themselves, now an
        agent on it ended its run without it."""
        self._outages.pop(runtime, None)

    def _lane_pause(self, stop: RuntimeStop, error: str) -> tuple[Paused, str, str]:
        """How long ``stop`` pauses its runtime, what's said of it, and what's said as it ends,
        counting an outage, with ``error``, among those in a row of its runtime."""
        runtime = stop.runtime
        if stop.failure.kind == "usage_limit":
            self._outages.pop(runtime, None)
            return (*self._limit_pause(stop), tasks_resuming_note(runtime))
        outage = self._outages[runtime] = outage_after(self._outages.get(runtime), runtime, error)
        return (*self._outage_pause(stop, outage), tasks_resuming_note(runtime))

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
        replaces, run the tasks waiting in the lanes that start by themselves, and start the work
        lane's turn, lifting a hold."""
        self.forget_wait()
        self._kick_lanes()
        await self._start(current_span(), asked=True)

    def _kick_lanes(self) -> None:
        """Run the tasks waiting in the lanes that start by themselves, if there are any."""
        if self._parts.lanes is not None:
            self._parts.lanes.kick()

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
        a runtime's usage limit or API not answering stopped them, wait to start again."""
        with use_span(cause):
            end = await work.run()
        if end in ("limited", "api_unavailable"):
            await self._wait.stop()  # The work lane's pause takes a lane's place.
        if end == "limited":
            assert work.runtime_stop is not None  # The usage limit stopped the run.
            self._wait_for_reset(work.runtime_stop, cause)
        elif end == "api_unavailable":
            assert work.runtime_stop is not None and work.outage is not None  # So did this.
            self._wait_out(work.runtime_stop, work.outage, cause)

    def _limit_pause(self, stop: RuntimeStop) -> tuple[Paused, str]:
        """How long the usage limit ``stop`` tells pauses its runtime, and what's said of it."""
        pause = self._pause
        at = limit_resume_time(stop, self._parts.usage(), pause.now(), pause.waits)
        return Paused(at, PAUSED_FOR_USAGE_LIMIT, stop.runtime), paused_note(at, stop.runtime)

    def _outage_pause(self, stop: RuntimeStop, outage: Outage) -> tuple[Paused, str]:
        """How long ``outage``, as ``stop`` tells it, pauses its runtime, after the backoff at
        its level unless the failure says when, and what's said of it."""
        at = outage_resume_time(stop, outage, self._pause.now())
        note = outage_paused_note(at, outage.error, stop.runtime)
        return Paused(at, PAUSED_FOR_API_UNAVAILABLE, stop.runtime), note

    def _wait_for_reset(self, stop: RuntimeStop, cause: Span) -> None:
        """Say when the run the usage limit ``stop`` tells stopped, started by ``cause``, starts
        again, and start it then."""
        paused, note = self._limit_pause(stop)
        self._pause_until(paused, note, _Restart(cause, RESUMING_NOTE))

    def _wait_out(self, stop: RuntimeStop, outage: Outage, cause: Span) -> None:
        """Say when the run ``outage``, as ``stop`` tells it, stopped, started by ``cause``,
        starts again, and start it then."""
        paused, note = self._outage_pause(stop, outage)
        self._pause_until(paused, note, _Restart(cause, outage_resuming_note(stop.runtime)))

    def _pause_until(self, paused: Paused, note: str, restart: _Restart) -> None:
        """Say ``note`` about the run that just stopped, and start it again as ``restart`` says
        once it's ``paused`` no longer."""
        stopped = self._turns.turn
        assert stopped is not None  # This runs as the turn stopped.
        self._wait_until(paused, note, restart, stopped)

    def _wait_until(
        self, paused: Paused, note: str, restart: _Restart, stopped: asyncio.Task[object] | None
    ) -> None:
        """Say ``note``, and start again as ``restart`` says once it's ``paused`` no longer and
        the ``stopped`` turn, if any, has ended."""
        self._parts.post(Notice(message=note))
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
        self, stopped: asyncio.Task[object] | None, at: datetime, restart: _Restart
    ) -> None:
        """Once the ``stopped`` turn, if any, has ended, wait until ``at``, then start the run
        again as ``restart`` says, or once the halt lifts if the session is halted then."""
        try:
            if stopped is not None:
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
        nothing keeps it waiting and work is left, and say so; either way it's paused no longer,
        so the other lanes start their tasks again."""
        self._pause_as(None)
        self._kick_lanes()
        work = self._startable(asked=False) if restart.work else None
        if not restart.work:
            self._parts.post(Notice(message=restart.note))
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
