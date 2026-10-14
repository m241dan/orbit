"""In-process implementation of :class:`orbit.protocol.Session`: the application's core.

It takes commands from the frontend, asks the domain what follows from them, drives the
ports to do it, and reports what happened as events.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Collection, Coroutine, Sequence
from contextlib import suppress
from typing import Any, assert_never

from orbit.agent.application.agent_run import AgentDesk, AgentRun, Agents, Outcome
from orbit.agent.application.landing import Lander
from orbit.agent.application.planner import PlannerChat
from orbit.agent.application.ports import TurnRunner
from orbit.agent.application.review_lane import REVIEW_ROLES, ReviewLane
from orbit.agent.application.verifier import Verification, Verifier
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.reviews import ReviewPlanner
from orbit.agent.domain.verification import FixAttempts, VerificationStep, fix_prompt
from orbit.protocol import (
    AcceptTask,
    AddTask,
    BusyChanged,
    CancelTurn,
    Command,
    EditTask,
    ErrorOccurred,
    Event,
    Notice,
    PlanningChanged,
    RemoveTask,
    RequestSuperReview,
    RunTasks,
    SetPlanning,
    Settings,
    SettingsChanged,
    SubmitPrompt,
    TasksChanged,
    TaskSpec,
    TaskStarted,
    TaskStatus,
    TurnEndReason,
    TurnFinished,
    UpdateSettings,
    VerificationChanged,
)


def _verifier(
    verification: Verification | None,
    board: TaskBoard,
    events: "asyncio.Queue[Event]",
    agents: Agents | None,
) -> Verifier | None:
    """What checks tasks on ``board`` (in the agents' tree, if any); none when
    ``verification`` has no steps."""
    if verification is None or not verification.steps:
        return None
    return Verifier(verification, board, events.put, agents.tree if agents else None)


class AgentSession:
    """Runs turns and task runs one at a time in the background and publishes their events.

    The session owns the task list and reports it whole on every change. A run
    goes through it in order; after each task that completes, the ``verification``
    steps run until one fails. A failure adds a task to fix it right after the
    failed one. Once a fix passes, the tasks it fixed count as completed.

    Tasks with tools off aren't verified, and neither are tasks that leave the
    working tree as it was. A ``verification`` without steps counts as none.
    """

    def __init__(
        self,
        runner: TurnRunner,
        settings: Settings | None = None,
        model_choices: Sequence[str] = (),
        verification: Verification | None = None,
        agents: Agents | None = None,
    ) -> None:
        self._runner = runner
        self._settings = settings or Settings()
        self._model_choices = tuple(model_choices)
        steps = verification.steps if verification else ()
        self._board = TaskBoard([s.name for s in steps])
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        self._verifier = _verifier(verification, self._board, self._events, agents)
        self._desk = AgentDesk(agents, self._events.put) if agents else None
        self._announced = False
        self._lane = self._review_lane()
        self._landed: dict[str, str] = {}  # Task id -> the commit it landed, until reported.
        self._starts: dict[str, str] = {}  # Task id -> the tree's head when it started.
        self._held = False  # The user cancelled: don't start work by itself.
        self._planning = False
        self._planner = self._planner_chat()
        self._events.put_nowait(self._settings_changed())
        if self._verifier:
            self._events.put_nowait(VerificationChanged(steps=self._board.steps))
        self._turn: asyncio.Task[object] | None = None

    @property
    def runner(self) -> TurnRunner:
        return self._runner

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def model_choices(self) -> tuple[str, ...]:
        return self._model_choices

    @property
    def agents(self) -> Agents | None:
        return self._desk.agents if self._desk else None

    @property
    def verification(self) -> Verification | None:
        return self._verifier.verification if self._verifier else None

    @property
    def _checks(self) -> Verifier:
        assert self._verifier is not None  # Only used while verifying a task.
        return self._verifier

    async def send(self, command: Command) -> None:
        match command:
            case SubmitPrompt() | CancelTurn() | UpdateSettings() | SetPlanning():
                await self._handle_conversation(command)
            case AddTask() | RunTasks():
                await self._handle_tasks(command)
            case EditTask() | AcceptTask() | RemoveTask():
                await self._handle_board(command)
            case RequestSuperReview():
                await self._super_review()
            case _:
                assert_never(command)

    async def _handle_conversation(
        self, command: SubmitPrompt | CancelTurn | UpdateSettings | SetPlanning
    ) -> None:
        match command:
            case SubmitPrompt(text=text):
                await self._submit(text)
            case SetPlanning(on=on):
                await self._set_planning(on)
            case CancelTurn():
                self._held = True
                self._cancel()
            case UpdateSettings(settings=settings):
                self._settings = settings
                await self._events.put(self._settings_changed())
            case _:
                assert_never(command)

    async def _submit(self, text: str) -> None:
        """Start a turn of the conversation, or of the planner's while planning."""
        runner = self._planner if self._planning and self._planner else self._runner
        await self._start(self._run_turn(runner, text, self._settings))

    async def _handle_tasks(self, command: AddTask | RunTasks) -> None:
        match command:
            case AddTask(task=task):
                self._board.add(task)
                await self._publish_tasks()
            case RunTasks():
                await self._run_tasks_if_possible()
            case _:
                assert_never(command)

    async def _handle_board(self, command: EditTask | AcceptTask | RemoveTask) -> None:
        match command:
            case EditTask(task_id=task_id, task=task):
                await self._change(self._board.edit, task_id, task)
            case AcceptTask(task_id=task_id):
                await self._change(self._board.accept, task_id)
            case RemoveTask(task_id=task_id):
                await self._change(self._board.remove, task_id)
            case _:
                assert_never(command)

    async def _run_tasks_if_possible(self) -> None:
        if self._desk is None:
            await self._events.put(ErrorOccurred(message="Tasks need agents, and none are set up."))
            return
        self._held = False
        if self._lane is not None:
            self._lane.kick()
        if self._next_work(()) is not None:
            await self._start(self._run_tasks())

    async def _super_review(self) -> None:
        if self._lane is None:
            message = "Super reviews need reviews turned on, inside a git repository."
            await self._events.put(ErrorOccurred(message=message))
        else:
            await self._lane.super_review()

    def _next_work(self, tried: Collection[str]) -> str | None:
        """The next task for the work lane; reviews have their own lane, if there is one."""
        exclude = REVIEW_ROLES if self._lane is not None else ()
        return self._board.next_runnable(skip=tried, exclude=exclude)

    async def _wake_work(self) -> None:
        """Run the work a review queued, unless something runs or the user cancelled."""
        busy = self._turn is not None and not self._turn.done()
        if not (busy or self._held) and self._next_work(()) is not None:
            await self._start(self._run_tasks())

    def _review_lane(self) -> ReviewLane | None:
        desk = self._desk
        if desk is None or not desk.agents.review or desk.agents.tree is None:
            return None
        agents = desk.agents
        planner = ReviewPlanner(
            agents.review_rounds,
            agents.review_suggestions,
            agents.super_review_every,
            agents.super_review_suggestions,
        )
        lane = ReviewLane(desk, self._board, planner, self._wake_work)
        desk.suggest = lane.suggest
        return lane

    def _cancel(self) -> None:
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()

    async def _change(self, change: Callable[..., None], *arguments: Any) -> None:
        """Change the board, publishing it, or report why the change was refused."""
        try:
            change(*arguments)
        except ValueError as exc:
            await self._events.put(ErrorOccurred(message=str(exc)))
        else:
            await self._publish_tasks()

    async def _set_planning(self, on: bool) -> None:
        if on and self._planner is None:
            message = "Planning needs agents and a planner role."
            await self._events.put(ErrorOccurred(message=message))
            return
        self._planning = on
        await self._events.put(PlanningChanged(on=on))

    def _planner_chat(self) -> PlannerChat | None:
        if self._desk is None or "planner" not in self._desk.agents.roles:
            return None
        return PlannerChat(self._desk, self._board)

    async def close(self) -> None:
        """Cancel the work in progress, which stops its agent, and stop the agents' host."""
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()
            with suppress(asyncio.CancelledError):
                await self._turn
        if self._lane is not None:
            await self._lane.close()
        if self._planner is not None:
            self._planner.close()
        if self._desk is not None:
            await self._desk.stop()

    async def events(self) -> AsyncIterator[Event]:
        if not self._announced:
            self._announced = True
            await self._announce_leftovers()
        while True:
            yield await self._events.get()

    async def _announce_leftovers(self) -> None:
        """Tell the user about work earlier runs left in the coder's tree."""
        tree = self._desk.agents.tree if self._desk else None
        for note in await tree.leftovers() if tree else []:
            await self._events.put(Notice(message=note))

    async def _start(self, work: Coroutine[Any, Any, object]) -> None:
        if self._turn is not None and not self._turn.done():
            work.close()
            await self._events.put(ErrorOccurred(message="A turn is already running."))
            return
        await self._events.put(BusyChanged(busy=True))
        self._turn = asyncio.create_task(work)
        # A callback, so it also runs if the work is cancelled before it starts.
        self._turn.add_done_callback(self._idle)

    def _idle(self, turn: "asyncio.Task[object]") -> None:
        # Work started since then reports its own end; staying quiet keeps it shown busy.
        if turn is self._turn:
            self._events.put_nowait(BusyChanged(busy=False))

    async def _run_tasks(self) -> None:
        tried: set[str] = set()
        attempts = FixAttempts()
        while (task_id := self._next_work(tried)) is not None:
            tried.add(task_id)
            if await self._run_task(task_id, attempts) == "cancelled":
                return

    async def _run_task(self, task_id: str, attempts: FixAttempts) -> TaskStatus:
        """Run one task through a new agent; returns its final status."""
        self._board.set_status(task_id, "running")
        await self._publish_tasks()
        if self._verifier:
            self._board.reset_steps()
            await self._checks.publish_steps()
        await self._events.put(TaskStarted(task_id=task_id))
        assert self._desk is not None  # Runs only start with agents.
        try:
            status = await self._run_agent(self._desk, task_id, attempts)
        except asyncio.CancelledError:
            if self._verifier:
                await self._verifier.cancel()
            status = "cancelled"
        except RuntimeError as exc:  # Git or the tree's setup failed.
            await self._events.put(ErrorOccurred(message=str(exc)))
            status = "error"
        self._board.finish(task_id, status)
        await self._publish_tasks()
        await self._report_to_review(task_id)
        return status

    async def _report_to_review(self, task_id: str) -> None:
        """Tell the review lane how task ``task_id`` ended."""
        if self._lane is None:
            return
        commit = self._landed.pop(task_id, None)
        if commit is not None:
            await self._lane.landed(task_id, commit)
        else:
            await self._lane.ended(task_id)

    async def _run_agent(self, desk: AgentDesk, task_id: str, attempts: FixAttempts) -> TaskStatus:
        """Run task ``task_id`` through a new agent and land its work; a fallback queues a fix
        for a fresh one."""
        root = await self._prepare(desk, task_id)
        task = self._board.get(task_id).task
        before = await self._verifier.state_before(task, root) if self._verifier else None
        outcome = await AgentRun(task_id, self._board, self._verifier, desk).run(before, root)
        if outcome.status == "completed":
            outcome = await self._land(desk, task_id, outcome, root)
        if outcome.failure is not None and outcome.status == "unverified":
            await self._queue_fix(task_id, attempts, *outcome.failure)
        return outcome.status

    async def _land(self, desk: AgentDesk, task_id: str, outcome: Outcome, root: str) -> Outcome:
        """Commit and merge a completed task's work, when there's a tree to do it from."""
        if desk.agents.tree is None:
            return outcome
        lander = Lander(desk.agents.tree, self._board, self._verifier, self._events.put)
        landing = await lander.land(task_id, outcome.summaries, root, self._starts.pop(task_id))
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
            await self._events.put(ErrorOccurred(message=message))
            return
        settings = self._board.get(task_id).task.settings
        fix = TaskSpec(prompt=fix_prompt(step, output), settings=settings)
        fix_id = self._board.add(fix, fixes=task_id)
        attempts.record(fix_id, task_id, step.name)
        if self._lane is not None:
            self._lane.joined(fix_id)

    async def _publish_tasks(self) -> None:
        await self._events.put(TasksChanged(tasks=self._board.tasks))

    async def _run_turn(self, runner: TurnRunner, text: str, settings: Settings) -> TurnEndReason:
        """Run one turn, publishing its events; returns how it ended."""
        turn_id = uuid.uuid4().hex
        reason: TurnEndReason = "completed"
        try:
            async for event in runner.run_turn(turn_id, text, settings):
                if isinstance(event, TurnFinished):
                    reason = event.reason
                await self._events.put(event)
        except asyncio.CancelledError:
            reason = "cancelled"
            await self._events.put(TurnFinished(turn_id=turn_id, reason=reason))
        except Exception as exc:
            reason = "error"
            await self._events.put(ErrorOccurred(turn_id=turn_id, message=str(exc)))
            await self._events.put(TurnFinished(turn_id=turn_id, reason=reason))
        return reason

    def _settings_changed(self) -> SettingsChanged:
        return SettingsChanged(settings=self._settings, model_choices=self._model_choices)
