"""The work commands: prompts to the conversational roles, tasks and the board, and the agents'
work."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import assert_never

from orbit.agent.application.conversation_seats import ConversationSeats
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.failures import command_failure
from orbit.agent.application.lane_runner import Lanes
from orbit.agent.application.lanes import OPEN_GATE, SessionGate
from orbit.agent.application.outbox import EventSink
from orbit.agent.application.proposals import FrontendReview
from orbit.agent.application.review_lane import ReviewLane
from orbit.agent.application.settings import SessionSettings
from orbit.agent.application.startup_check import AGENT_ID as STARTUP_AGENT
from orbit.agent.application.startup_check import StartupCheck
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.work_turns import WorkTurns
from orbit.agent.domain.agents import Agent
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.catalog import trigger_names
from orbit.agent.domain.refusals import Refused, unknown
from orbit.agent.domain.roles import Role, check_enabled
from orbit.protocol import (
    CONFIGURE_CHECKS,
    INVALID,
    NOT_CONFIGURED,
    SUPER_REVIEW,
    UNKNOWN,
    AcceptTask,
    AddTask,
    CancelAgent,
    CancelTurn,
    DecideProposal,
    EditTask,
    ErrorOccurred,
    FireTrigger,
    MessageSent,
    ProposalDecided,
    RemoveTask,
    ResetConversation,
    RunTasks,
    SendMessage,
    Settings,
    TaskSpec,
    TaskState,
    UpdateSettings,
)
from orbit.tracing import Span, current_span, use_span

WorkCommand = (
    SendMessage
    | CancelTurn
    | UpdateSettings
    | ResetConversation
    | AddTask
    | RunTasks
    | EditTask
    | AcceptTask
    | RemoveTask
    | FireTrigger
    | CancelAgent
    | DecideProposal
)

QueuedWork = RunTasks | FireTrigger
"""The work commands that start work, which a read-only orbit queues rather than runs."""


@dataclass(frozen=True)
class CommandParts:
    """What the work commands act on: the session's ``settings``, the ``events`` it publishes
    into, its ``tasks``, its conversational roles' ``seats``, the work lane's ``turns``, its
    review ``lane``, if any, the ``proposals`` waiting for the user, the ``desk`` running its
    agents and the ``startup`` check, if any; ``save`` saves the session and ``reviews_changed``
    saves its review state; the session's ``gate`` says whether another orbit owns the
    repository; and its ``lanes`` are the lanes work runs in, by name."""

    settings: SessionSettings
    events: EventSink
    tasks: Tasks
    seats: ConversationSeats
    turns: WorkTurns
    lane: ReviewLane | None
    proposals: FrontendReview
    desk: AgentDesk | None
    startup: StartupCheck | None
    save: Callable[[], Awaitable[None]]
    reviews_changed: Callable[[], Awaitable[None]]
    gate: SessionGate = OPEN_GATE
    lanes: Lanes = field(default_factory=Lanes)


class WorkCommands:
    """Handles the work commands on the session held in ``parts``; while it's read-only, or
    taking ownership, those that start work are queued, in order, rather than run, each later
    run in the span current as it was queued, its own command's, so what it starts links
    there. A task added, accepted or edited starts at once if its lane starts by itself; one
    added or edited into a disabled role is refused, as is a trigger whose role is disabled."""

    def __init__(self, parts: CommandParts) -> None:
        self._parts = parts
        self._queued: list[tuple[QueuedWork, Span]] = []  # With the span current as queued.
        self._owning = False  # Taking ownership: queued work hasn't all run yet.

    @property
    def queued(self) -> tuple[QueuedWork, ...]:
        """The commands queued while read-only, oldest first."""
        return tuple(command for command, _ in self._queued)

    def drop_queued(self) -> int:
        """Drop the commands queued while read-only, as the session they were asked in was
        left; returns how many there were."""
        dropped = len(self._queued)
        self._queued.clear()
        return dropped

    async def handle(self, command: WorkCommand, command_id: str) -> None:
        """Handle ``command``, sent as ``command_id``, or queue it if it starts work while
        read-only or taking ownership; work that couldn't run once this orbit owns the
        repository is refused. Work left queued by a takeover that stopped early runs first."""
        queueing = self._owning or self._parts.gate.read_only()
        if queueing and isinstance(command, QueuedWork):
            self._check(command)
            self._queued.append((command, current_span()))
            return
        if self._queued:
            self.taking()  # Work sent meanwhile queues behind what's left.
            await self.own(command_id)
        await self._dispatch(command, command_id)

    def taking(self) -> None:
        """Keep queueing work, as this orbit is taking ownership of the repository, until
        :meth:`own` has run what's queued or :meth:`taken` says the takeover stopped."""
        self._owning = True

    def taken(self) -> None:
        """Stop queueing work for the takeover: it's over."""
        self._owning = False

    async def own(self, command_id: str) -> None:
        """Run the commands queued while read-only or taking ownership, in order, for
        ``command_id``, as this orbit owns the repository now, those queued meanwhile included;
        one that fails is reported as an error, and the rest still run. Each is taken off the
        queue as it's run, so those not yet run stay queued if this stops early, until the
        next command once owning runs them."""
        try:
            while self._queued:
                command, cause = self._queued.pop(0)
                with use_span(cause):
                    await self._run_queued(command, command_id)
        finally:
            self.taken()

    async def _run_queued(self, command: QueuedWork, command_id: str) -> None:
        """Run the queued ``command`` for ``command_id``, reporting a refusal, or any other
        failure, as an error, as nothing waits on its answer now."""
        try:
            await self._dispatch(command, command_id)
            return
        except Exception as exc:
            code, message = command_failure(command, exc)
        await self._parts.events.put(ErrorOccurred(code=code, message=message))

    def _check(self, command: QueuedWork) -> None:
        """Refuse ``command`` as running it would, for what doesn't change with ownership."""
        match command:
            case RunTasks():
                self._parts.turns.check_run_tasks()
            case FireTrigger():
                self._check_trigger(command)
            case _:
                assert_never(command)

    async def _dispatch(self, command: WorkCommand, command_id: str) -> None:
        match command:
            case SendMessage() | CancelTurn() | UpdateSettings():
                await self._handle_prompts(command, command_id)
            case AddTask() | RunTasks():
                await self._handle_tasks(command)
            case EditTask() | AcceptTask() | RemoveTask():
                await self._handle_board(command)
            case FireTrigger() | CancelAgent() | DecideProposal() | ResetConversation():
                await self._handle_agents(command, command_id)
            case _:
                assert_never(command)

    async def _handle_prompts(
        self, command: SendMessage | CancelTurn | UpdateSettings, command_id: str
    ) -> None:
        match command:
            case SendMessage():
                await self._send_message(command, command_id)
            case CancelTurn(lane=lane):
                await self._cancel_turn(lane)
            case UpdateSettings(settings=settings, role=role):
                await self._update_settings(settings, role)
            case _:
                assert_never(command)

    async def _update_settings(self, settings: Settings, role: str) -> None:
        """Have the conversational role ``role`` use ``settings``, publishing them, then save;
        refused for a role without a seat."""
        problem = f"Role {role!r} has no settings of its own"
        self._parts.seats.seat(role, problem, "the roles with them are")
        self._parts.settings.update(settings, role)
        await self._parts.events.put(self._parts.settings.changed(role))
        await self._parts.save()

    async def _send_message(self, command: SendMessage, command_id: str) -> None:
        """Start a turn of the conversational role the message names with its text, with that
        role's settings, publishing the message once accepted; refused for any other agent."""
        problem = f"Agent {command.agent!r} can't be messaged"
        seat = self._parts.seats.seat(command.agent, problem, "the agents you can message are")
        await seat.submit(command.text, self._parts.settings.of(command.agent))
        # Put at once, before the turn just started runs, so the log has it ahead of the answer.
        self._parts.events.put_nowait(
            MessageSent(command_id=command_id, agent=command.agent, text=command.text)
        )

    def _roles(self) -> Mapping[str, Role]:
        """The session's roles; none without agents."""
        desk = self._parts.desk
        return desk.agents.roles if desk else {}

    async def _handle_tasks(self, command: AddTask | RunTasks) -> None:
        match command:
            case AddTask(task=task):
                check_enabled(self._roles(), task.role)
                await self._parts.tasks.change(lambda board: board.add(task))
                self._parts.lanes.kick_for(task.role)
            case RunTasks():
                await self._parts.turns.run_tasks()
            case _:
                assert_never(command)

    async def _handle_board(self, command: EditTask | AcceptTask | RemoveTask) -> None:
        match command:
            case EditTask(task_id=task_id, task=task):
                await self._edit(task_id, task)
            case AcceptTask(task_id=task_id):
                await self._accept(task_id)
            case RemoveTask(task_id=task_id):
                await self._remove(task_id)
            case _:
                assert_never(command)

    async def _accept(self, task_id: str) -> None:
        """Accept a draft, if it is one, and start it if its lane starts by itself."""

        def accept(board: TaskBoard) -> TaskState:
            board.accept(task_id)
            return board.get(task_id)

        state = await self._parts.tasks.change(accept)
        self._parts.lanes.kick_for(state.task.role)

    async def _edit(self, task_id: str, task: TaskSpec) -> None:
        """Edit a task, if the board allows it and its new role isn't disabled, and start it if
        its lane starts by itself. An edited task in a review batch has to finish again before
        the batch does."""
        check_enabled(self._roles(), task.role)
        lane = self._parts.lane

        def edit(board: TaskBoard) -> TaskState:
            board.edit(task_id, task)
            return board.get(task_id)

        # The edited task is taken from the change: it may be removed while the save is awaited.
        state = await self._parts.tasks.change(edit)
        self._parts.lanes.kick_for(state.task.role)
        if lane is not None:
            lane.edited(state)
            await self._parts.reviews_changed()

    async def _remove(self, task_id: str) -> None:
        """Remove a task, and take it out of its review batch, if any."""
        state = self._parts.tasks.board.find(task_id)
        await self._parts.tasks.change(lambda board: board.remove(task_id))
        if self._parts.lane is not None:
            assert state is not None  # The board removed it, so it was there.
            await self._parts.lane.removed(task_id, state)

    async def _cancel_turn(self, lane: str) -> None:
        """Stop the work in ``lane``: the work lane's run, holding it; the task running in a lane
        that starts by itself, which carries on; or the turn of the conversational role
        answering in it. Refused for a lane there's none of."""
        lanes = self._parts.lanes
        if lane not in lanes.names:
            raise unknown(f"No lane {lane!r}", "the lanes are", lanes.names)
        runner = lanes.runner(lane)
        if lane == lanes.work:
            self._parts.turns.hold()
        elif runner is not None:
            runner.cancel()
        else:
            self._parts.seats.cancel(lane)

    async def _handle_agents(
        self,
        command: FireTrigger | CancelAgent | DecideProposal | ResetConversation,
        command_id: str,
    ) -> None:
        match command:
            case CancelAgent(agent_id=agent_id):
                await self._cancel_agent(agent_id)
            case DecideProposal():
                await self._decide(command, command_id)
            case FireTrigger():
                await self._fire_trigger(command)
            case ResetConversation():
                await self._reset_conversation(command)
            case _:
                assert_never(command)

    async def _reset_conversation(self, command: ResetConversation) -> None:
        """Reset the conversation of the conversational role the command names; refused for
        any other agent."""
        problem = f"Agent {command.agent!r} has no conversation to reset"
        seat = self._parts.seats.seat(command.agent, problem, "the agents with one are")
        await seat.reset()

    async def _fire_trigger(self, command: FireTrigger) -> None:
        """Run the built-in trigger ``command`` names; refused for any other name, one whose role
        is disabled, or any input, since the built-ins take none."""
        self._check_trigger(command)
        configure = self._parts.turns.configure_checks
        runs = {SUPER_REVIEW: self._super_review, CONFIGURE_CHECKS: configure}
        await runs[command.trigger]()

    def _check_trigger(self, command: FireTrigger) -> None:
        """Refuse a trigger that isn't built in, or whose role is disabled, as there's none of
        it; one given input; or one that couldn't run here."""
        names = trigger_names(self._roles())
        if command.trigger not in names:
            raise unknown(f"No trigger {command.trigger!r}", "the triggers are", names)
        if command.input:
            raise Refused(INVALID, f"The {command.trigger} trigger takes no input.")
        if command.trigger == SUPER_REVIEW:
            self._check_super_review()
        else:
            self._parts.turns.check_configure_checks()

    async def _decide(self, command: DecideProposal, command_id: str) -> None:
        """Settle the proposal ``command`` decides and publish the decision; refused when none
        is waiting."""
        proposal = self._parts.proposals.decide(command)
        if proposal is None:
            message = f"No proposal is waiting for task {command.task_id!r}."
            raise Refused(UNKNOWN, message)
        decided = ProposalDecided(
            command_id=command_id,
            agent_id=proposal.agent_id,
            task_id=command.task_id,
            action=command.action,
            feedback=command.feedback,
        )
        await self._parts.events.put(decided)

    async def _cancel_agent(self, agent_id: str) -> None:
        """Stop the startup check, or the task of an agent in a lane that starts by itself, on its
        own; any other agent's work, like ctrl+s."""
        if self._cancel_startup(agent_id):
            return
        desk = self._parts.desk
        agent = desk.find(agent_id) if desk else None
        if agent is None:
            raise Refused(UNKNOWN, f"No running agent {agent_id!r}.")
        self._cancel_work_of(agent)

    def _cancel_work_of(self, agent: Agent) -> None:
        """Stop the task of an agent in a lane that starts by itself, such as a reviewer's review,
        or a conversational role's turn, on its own; else the work lane's."""
        runner = self._parts.lanes.of_role(agent.role)
        seat = self._parts.seats.get(agent.role)
        if runner is not None:
            runner.cancel(agent.task_id)
        elif seat is not None:
            seat.cancel()
        else:
            self._parts.turns.hold()

    def _cancel_startup(self, agent_id: str) -> bool:
        """Cancel the startup check if it's agent ``agent_id`` and still running; returns
        whether it was."""
        startup = self._parts.startup
        if agent_id != STARTUP_AGENT or startup is None or not startup.running:
            return False
        startup.cancel()
        return True

    def _check_super_review(self) -> None:
        """Refuse a super review without reviews."""
        if self._parts.lane is None:
            message = "Super reviews need reviews turned on, inside a git repository."
            raise Refused(NOT_CONFIGURED, message)

    async def _super_review(self) -> None:
        assert self._parts.lane is not None  # _check_trigger refuses a super review without.
        await self._parts.lane.super_review()
