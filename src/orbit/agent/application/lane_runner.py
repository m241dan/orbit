"""The lanes that start by themselves: every lane among the enabled task roles' but the one whose
roles merge, which runs on the user's asking. Each runs the board's next runnable task whose role
is in it, one at a time, through a new agent, once a task for it is queued, unless this orbit is
read-only, the session is halted or the work lane waits out a pause. Reviews run so too, with the
rules :class:`ReviewLane` keeps."""

import asyncio
from collections.abc import Awaitable, Callable, Collection, Coroutine, Iterable
from dataclasses import dataclass
from functools import partial
from typing import Any, Protocol

from orbit.agent.application.agent_run import AgentRun
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.lanes import Background, SessionGate
from orbit.agent.application.outcomes import Outcome, UserStop, run_task_to_end
from orbit.agent.application.role_workspace import workspace_for
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.endings import STOPPED_BY_YOU
from orbit.protocol import PLANNER_LANE, WORK_LANE, BusyChanged, BusyLane, Event


def unpaused() -> bool:
    """Says the work lane waits out no pause."""
    return False


def planners_lane() -> tuple[BusyLane, ...]:
    """Says the conversational roles answer in the planner's lane alone."""
    return (PLANNER_LANE,)


def unposted(event: Event) -> None:
    """Posts ``event`` nowhere."""


def _unwatched() -> None:
    """Note an end no one watches."""


@dataclass(frozen=True)
class LaneParts:
    """What a lane that starts by itself runs with: ``desk``'s agents and the session's
    ``tasks``; its ``gate``, which says whether another orbit owns the repository or the session
    is halted, and ``paused``, whether the work lane waits out a pause, when no task starts; and
    ``post``, which publishes, without blocking, whether the lane is busy."""

    desk: AgentDesk
    tasks: Tasks
    gate: SessionGate
    paused: Callable[[], bool] = unpaused
    post: Callable[[Event], None] = unposted


@dataclass(frozen=True)
class LaneWork:
    """What a lane runs: ``next_task`` is the task to run next, skipping those given, ``run``
    runs one to its end, and ``spent`` are the tasks not to run again."""

    next_task: Callable[[Collection[str]], str | None]
    run: Callable[[str], Awaitable[None]]
    spent: Callable[[], set[str]] = set


class LaneRunner:
    """Runs lane ``lane``'s tasks, those whose role is among ``roles``, one at a time in the
    background with ``parts``.

    Each task's agent works in its role's workspace, unless ``work`` says what to run and how.
    The lane shows busy from when its run picks a task until the run ends; the user's
    :meth:`cancel` stops the task running, and the lane carries on with the next."""

    def __init__(
        self, lane: BusyLane, roles: Collection[str], parts: LaneParts, work: LaneWork | None = None
    ) -> None:
        self.lane = lane
        self.roles = frozenset(roles)
        self._desk = parts.desk
        self._tasks = parts.tasks
        self._parts = parts
        self._work = work or LaneWork(self._next_in_lane, self.run_in_workspace)
        self._background = Background(lane, parts.desk.publish)
        self._background.ended = self._ended
        self._stopped: Callable[[], None] = _unwatched
        self._running: str | None = None  # The task running.
        self._busy = False  # The lane was last shown busy.
        # The agent's run of the task running, which the user's stop cancels.
        self._agent: tuple[str, asyncio.Task[Outcome]] | None = None

    @property
    def running(self) -> bool:
        """Whether the lane's tasks are being run."""
        return self._background.running

    @property
    def busy(self) -> bool:
        """Whether the lane shows busy: its run has picked a task and not ended yet."""
        return self._busy

    @property
    def current(self) -> str | None:
        """The task running or, before the lane picks one, the one it will pick; none when
        there's none."""
        return self._running or self._work.next_task(self._work.spent())

    def on_stopped(self, stopped: Callable[[], None]) -> None:
        """Have ``stopped`` called whenever the lane's tasks stop running."""
        self._stopped = stopped

    def kick(self) -> None:
        """Run the lane's tasks waiting, unless they're being run already or no task may start;
        those queued meanwhile wait on the board."""
        if not (self._blocked() or self.running):
            self._background.start(self._run_tasks())

    def cancel(self, task_id: str | None = None) -> None:
        """Stop the task running if it's ``task_id``, or whichever it is if none is named; the
        lane carries on."""
        agent = self._agent
        if agent is not None and task_id in (None, agent[0]):
            agent[1].cancel()

    async def stop(self) -> None:
        """Stop the lane's tasks running, and wait for them to; they run again once kicked."""
        await self._background.stop()

    async def close(self) -> None:
        """Stop the lane's tasks running for good: a kick from now on runs none."""
        await self._background.close()

    async def alone(self, task_id: str, run: Callable[[], Coroutine[Any, Any, Outcome]]) -> Outcome:
        """How ``run``, task ``task_id``'s run through its agent, ended, run as a task of its own
        that :meth:`cancel` cancels, leaving the lane's run going; anything unexpected
        escapes."""
        agent = asyncio.create_task(run())
        self._agent = (task_id, agent)
        try:
            return await agent
        finally:
            self._agent = None

    async def run_in_workspace(self, task_id: str) -> None:
        """Run task ``task_id`` to its end through a new agent, as :meth:`_run_agent` does; the
        user's stop ends it as stopped by them."""
        run = partial(self.alone, task_id, partial(self._run_agent, task_id))
        stop = UserStop(STOPPED_BY_YOU)
        await run_task_to_end(self._tasks, task_id, run, stop, self._desk.publish)

    def _blocked(self) -> bool:
        """Whether no task may start: this orbit is read-only, the session is halted or the work
        lane waits out a pause."""
        gate = self._parts.gate
        return gate.read_only() or gate.halted() or self._parts.paused()

    def _show_busy(self, busy: bool) -> None:
        """Show the lane ``busy`` or not, unless it's shown so already."""
        if busy != self._busy:
            self._busy = busy
            self._parts.post(BusyChanged(busy=busy, lane=self.lane))

    def _ended(self) -> None:
        """The lane's tasks stopped running: show it's no longer busy, unless a run started
        since, which goes on showing it, and tell whoever watches."""
        if not self.running:
            self._show_busy(False)
        self._stopped()

    async def _run_tasks(self) -> None:
        """Run the lane's tasks waiting, one at a time, until none are left or no task may
        start, showing the lane busy once it picks one."""
        tried = self._work.spent()
        while (task_id := self._work.next_task(tried)) is not None:
            if self._blocked():  # It may have halted, or paused, as the last task ended.
                return
            self._show_busy(True)
            tried.add(task_id)
            self._running = task_id
            try:
                await self._work.run(task_id)
            finally:
                self._running = None

    def _next_in_lane(self, tried: Collection[str]) -> str | None:
        """The board's next task in one of the lane's roles, skipping those ``tried``."""
        return self._tasks.board.next_runnable(skip=tried, roles=self.roles)

    async def _run_agent(self, task_id: str) -> Outcome:
        """How task ``task_id``'s run through a new agent ended, in its role's workspace."""
        run = AgentRun(task_id, self._tasks, None, self._desk).run
        role = self._desk.agents.roles[self._tasks.board.get(task_id).task.role]
        async with workspace_for(role.workspace, self._desk) as root:
            return await run(None, root)


class Kickable(Protocol):
    """What runs lanes that start by themselves, kicked to run the tasks waiting."""

    def kick(self) -> None: ...


class Lanes:
    """The session's lanes, by name: ``work``, the lane whose roles merge, which runs on the
    user's asking, those the conversational roles answer in, as ``conversing`` says each time
    they're named, the planner's unless given, and those the ``runners`` run, which start by
    themselves."""

    def __init__(
        self,
        work: BusyLane = WORK_LANE,
        runners: Iterable[LaneRunner] = (),
        conversing: Callable[[], Iterable[BusyLane]] = planners_lane,
    ) -> None:
        self.work = work
        self._runners = {runner.lane: runner for runner in runners}
        self._conversing = conversing

    @property
    def names(self) -> tuple[BusyLane, ...]:
        """Every lane's name: the work lane's, the conversational roles', then those that start
        by themselves."""
        return (self.work, *self._conversing(), *self._runners)

    @property
    def elsewhere(self) -> frozenset[str]:
        """The roles whose tasks the lanes that start by themselves run."""
        return frozenset(role for runner in self._runners.values() for role in runner.roles)

    def runner(self, lane: str) -> LaneRunner | None:
        """The runner of lane ``lane``; none if it doesn't start by itself."""
        return self._runners.get(lane)

    def of_role(self, role: str) -> LaneRunner | None:
        """The runner of the lane that runs role ``role``'s tasks; none if it doesn't start by
        itself."""
        return next((r for r in self._runners.values() if role in r.roles), None)

    def busy(self) -> dict[BusyLane, bool]:
        """Whether each lane that starts by itself is busy, by name."""
        return {lane: runner.busy for lane, runner in self._runners.items()}

    def kick(self) -> None:
        """Run the tasks waiting in every lane that starts by itself."""
        for runner in self._runners.values():
            runner.kick()

    def kick_for(self, role: str) -> None:
        """Run the tasks waiting in role ``role``'s lane, if it starts by itself."""
        runner = self.of_role(role)
        if runner is not None:
            runner.kick()
