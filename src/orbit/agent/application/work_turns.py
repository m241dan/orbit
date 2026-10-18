"""When the work lane runs: on the user's asking, after a review queues work unless the user
held it or the session halted, and first for a configurator."""

from collections.abc import Callable
from dataclasses import dataclass

from orbit.agent.application.desk import AgentDesk, Agents
from orbit.agent.application.lanes import TurnSlot
from orbit.agent.application.review_lane import ReviewLane
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.verifier import Verifier
from orbit.agent.application.work_lane import WorkLane
from orbit.agent.domain.checks import configure_prompt
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.roles import role_task
from orbit.protocol import CONFIGURATOR, NOT_CONFIGURED, WORK_LANE, Event, Notice, TaskSpec

NO_AGENTS_MESSAGE = "Tasks need agents, and none are set up."
NO_VERIFY_MESSAGE = "Setting up checks needs verification on; orbit was started with --no-verify."
CONFIGURING_NOTE = "A configurator task is already queued or running."
TASKS_RUNNING_MESSAGE = "Tasks are already running."


@dataclass(frozen=True)
class WorkParts:
    """What decides when the work lane runs: the ``work`` lane and review ``lane``, if any, the
    ``tasks`` they run, the ``verifier`` and agents' ``desk``, if any, ``post``, which
    publishes events without blocking, and ``halted``, which tells whether the session is
    halted."""

    work: WorkLane | None
    lane: ReviewLane | None
    tasks: Tasks
    verifier: Verifier | None
    desk: AgentDesk | None
    post: Callable[[Event], None]
    halted: Callable[[], bool]


class WorkTurns:
    """Runs the work lane's turns from ``parts``, one at a time.

    The user's :meth:`run_tasks` starts a run, refused while one runs; :meth:`hold` cancels it
    and keeps :meth:`wake`, a review's request, from starting another until the user asks
    again. While the session is halted, :meth:`wake` starts nothing either.
    """

    def __init__(self, parts: WorkParts) -> None:
        self._parts = parts
        self._board = parts.tasks.board
        self._held = False  # The user cancelled: don't start work by itself.
        self._turns = TurnSlot(WORK_LANE, TASKS_RUNNING_MESSAGE, parts.post)

    @property
    def running(self) -> bool:
        return self._turns.running

    async def run_tasks(self) -> None:
        """Start a run for the user, lifting a hold; refused without agents, or while one
        runs."""
        if self._parts.work is None:
            raise Refused(NOT_CONFIGURED, NO_AGENTS_MESSAGE)
        if self._parts.lane is not None:
            self._parts.lane.kick()
        await self._start(asked=True)

    async def configure_checks(self) -> None:
        """Queue a configurator first and run it, unless work is running already (then it goes
        next); refused without verification or while one is queued or running."""
        desk, verifier = self._parts.desk, self._parts.verifier
        if desk is None or self._parts.work is None:
            raise Refused(NOT_CONFIGURED, NO_AGENTS_MESSAGE)
        if verifier is None:
            raise Refused(NOT_CONFIGURED, NO_VERIFY_MESSAGE)
        if self._board.has_waiting(CONFIGURATOR):
            self._parts.post(Notice(message=CONFIGURING_NOTE))
            return
        task = _configurator_task(desk.agents, verifier)
        await self._parts.tasks.change(lambda board: board.add_first(task))
        if not self.running:
            await self._start(asked=True)

    def hold(self) -> None:
        """Cancel the run, and hold the lane until the user asks for work again."""
        self._held = True
        if self._turns.cancel():
            assert self._parts.work is not None  # Only the work lane takes work turns.
            self._parts.work.stopped_by_user()

    async def wake(self) -> None:
        """Run the work a review queued, unless something runs, the user cancelled or the
        session is halted."""
        await self._start(asked=False)

    async def close(self) -> None:
        """Cancel the run, if any, and wait for it to end."""
        await self._turns.close()

    @property
    def _waits(self) -> bool:
        """Whether work not asked for waits: held, already running or halted."""
        return self._held or self.running or self._parts.halted()

    async def _start(self, *, asked: bool) -> None:
        """Start the work lane's turn if there is work. The user ``asked`` lifts a hold, and a
        run already running refuses them; otherwise a hold, a running run or a halt keeps it
        waiting."""
        work = self._parts.work
        assert work is not None  # Work needs agents.
        if asked:
            self._held = False
        elif self._waits:
            return
        if work.next_work() is not None:
            await self._turns.start(work.run())


def _configurator_task(agents: Agents, verifier: Verifier) -> TaskSpec:
    """A configurator's task, told the steps and setup configured now."""
    steps = verifier.verification.steps
    prompt = configure_prompt(steps, agents.tree.setup if agents.tree else None)
    return role_task(CONFIGURATOR, agents.roles[CONFIGURATOR], prompt)
