"""The work commands: prompts to the planner, tasks and the board, and the agents' work."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import assert_never

from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.lanes import OPEN_GATE, SessionGate
from orbit.agent.application.outbox import EventSink
from orbit.agent.application.planner_seat import PlannerSeat
from orbit.agent.application.proposals import FrontendReview
from orbit.agent.application.review_lane import ReviewLane
from orbit.agent.application.settings import SessionSettings
from orbit.agent.application.startup_check import AGENT_ID as STARTUP_AGENT
from orbit.agent.application.startup_check import StartupCheck
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.work_turns import WorkTurns
from orbit.agent.domain.agents import Agent
from orbit.agent.domain.catalog import CONVERSATIONAL_ROLES, LANES, TRIGGER_NAMES
from orbit.agent.domain.refusals import Refused, unknown
from orbit.agent.domain.roles import REVIEW_ROLES
from orbit.protocol import (
    CONFIGURE_CHECKS,
    INTERNAL,
    INVALID,
    NOT_CONFIGURED,
    PLANNER,
    SUPER_REVIEW,
    UNKNOWN,
    WORK_LANE,
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
    TaskSpec,
    UpdateSettings,
)

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
    into, its ``tasks``, its planner ``seat``, the work lane's ``turns``, its review ``lane``,
    if any, the ``proposals`` waiting for the user, the ``desk`` running its agents and the
    ``startup`` check, if any; ``save`` saves the session and ``reviews_changed`` saves its
    review state; the session's ``gate`` says whether another orbit owns the repository."""

    settings: SessionSettings
    events: EventSink
    tasks: Tasks
    seat: PlannerSeat
    turns: WorkTurns
    lane: ReviewLane | None
    proposals: FrontendReview
    desk: AgentDesk | None
    startup: StartupCheck | None
    save: Callable[[], Awaitable[None]]
    reviews_changed: Callable[[], Awaitable[None]]
    gate: SessionGate = OPEN_GATE


class WorkCommands:
    """Handles the work commands on the session held in ``parts``; while it's read-only, those
    that start work are queued, in order, rather than run."""

    def __init__(self, parts: CommandParts) -> None:
        self._parts = parts
        self._queued: list[QueuedWork] = []

    @property
    def queued(self) -> tuple[QueuedWork, ...]:
        """The commands queued while read-only, oldest first."""
        return tuple(self._queued)

    async def handle(self, command: WorkCommand, command_id: str) -> None:
        """Handle ``command``, sent as ``command_id``, or queue it if it starts work while
        read-only; work that couldn't run once this orbit owns the repository is refused."""
        if self._parts.gate.read_only() and isinstance(command, QueuedWork):
            self._check(command)
            self._queued.append(command)
        else:
            await self._dispatch(command, command_id)

    async def own(self, command_id: str) -> None:
        """Run the commands queued while read-only, in order, for ``command_id``, as this orbit
        owns the repository now; one that fails is reported as an error, and the rest still
        run. Each is taken off the queue as it's run, so those not yet run stay queued if
        this stops early."""
        while self._queued:
            await self._run_queued(self._queued.pop(0), command_id)

    async def _run_queued(self, command: QueuedWork, command_id: str) -> None:
        """Run the queued ``command`` for ``command_id``, reporting a refusal, or any other
        failure, as an error, as nothing waits on its answer now."""
        try:
            await self._dispatch(command, command_id)
            return
        except Refused as refusal:
            error = ErrorOccurred(code=refusal.code, message=refusal.message)
        except Exception as exc:
            message = f"Orbit failed to handle {type(command).__name__}: {exc}"
            error = ErrorOccurred(code=INTERNAL, message=message)
        await self._parts.events.put(error)

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
            case UpdateSettings(settings=settings):
                self._parts.settings.update(settings)
                await self._parts.events.put(self._parts.settings.changed())
                await self._parts.save()
            case _:
                assert_never(command)

    async def _send_message(self, command: SendMessage, command_id: str) -> None:
        """Start a planner turn with the message's text, publishing the message once accepted;
        refused for any other agent."""
        if command.agent not in CONVERSATIONAL_ROLES:
            problem = f"Agent {command.agent!r} can't be messaged"
            raise unknown(problem, "the agents you can message are", CONVERSATIONAL_ROLES)
        await self._parts.seat.submit(command.text, self._parts.settings.current)
        # Put at once, before the turn just started runs, so the log has it ahead of the answer.
        self._parts.events.put_nowait(
            MessageSent(command_id=command_id, agent=command.agent, text=command.text)
        )

    async def _handle_tasks(self, command: AddTask | RunTasks) -> None:
        match command:
            case AddTask(task=task):
                await self._parts.tasks.change(lambda board: board.add(task))
            case RunTasks():
                await self._parts.turns.run_tasks()
            case _:
                assert_never(command)

    async def _handle_board(self, command: EditTask | AcceptTask | RemoveTask) -> None:
        match command:
            case EditTask(task_id=task_id, task=task):
                await self._edit(task_id, task)
            case AcceptTask(task_id=task_id):
                await self._parts.tasks.change(lambda board: board.accept(task_id))
            case RemoveTask(task_id=task_id):
                await self._remove(task_id)
            case _:
                assert_never(command)

    async def _edit(self, task_id: str, task: TaskSpec) -> None:
        """Edit a task, unless it's a review orbit queued: its batch is spent, so it couldn't
        run again. An edited task in a review batch has to finish again before the batch does."""
        lane = self._parts.lane
        if lane is not None and lane.queued_by_orbit(task_id):
            raise Refused(INVALID, "Reviews can't be edited; remove it instead.")
        await self._parts.tasks.change(lambda board: board.edit(task_id, task))
        if lane is not None:
            lane.edited(task_id)
            await self._parts.reviews_changed()

    async def _remove(self, task_id: str) -> None:
        """Remove a task, and take it out of its review batch, if any."""
        state = self._parts.tasks.board.find(task_id)
        await self._parts.tasks.change(lambda board: board.remove(task_id))
        if self._parts.lane is not None:
            assert state is not None  # The board removed it, so it was there.
            await self._parts.lane.removed(task_id, state)

    async def _cancel_turn(self, lane: str) -> None:
        """Stop the work in ``lane``, or say there's no such lane."""
        if lane not in LANES:
            raise unknown(f"No lane {lane!r}", "the lanes are", LANES)
        if lane == WORK_LANE:
            self._parts.turns.hold()
        else:
            self._parts.seat.cancel()

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
        """Reset the planner's conversation; refused for any other agent."""
        if command.agent not in CONVERSATIONAL_ROLES:
            problem = f"Agent {command.agent!r} has no conversation to reset"
            raise unknown(problem, "the agents with one are", CONVERSATIONAL_ROLES)
        await self._parts.seat.reset()

    async def _fire_trigger(self, command: FireTrigger) -> None:
        """Run the built-in trigger ``command`` names; refused for any other name, or any input,
        since the built-ins take none."""
        self._check_trigger(command)
        configure = self._parts.turns.configure_checks
        runs = {SUPER_REVIEW: self._super_review, CONFIGURE_CHECKS: configure}
        await runs[command.trigger]()

    def _check_trigger(self, command: FireTrigger) -> None:
        """Refuse a trigger that isn't built in, given input, or that couldn't run here."""
        if command.trigger not in TRIGGER_NAMES:
            raise unknown(f"No trigger {command.trigger!r}", "the triggers are", TRIGGER_NAMES)
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
        """Stop the startup check, or a reviewer's review, on its own; any other agent's work,
        like ctrl+s."""
        if self._cancel_startup(agent_id):
            return
        desk = self._parts.desk
        agent = desk.find(agent_id) if desk else None
        if agent is None:
            raise Refused(UNKNOWN, f"No running agent {agent_id!r}.")
        self._cancel_work_of(agent)

    def _cancel_work_of(self, agent: Agent) -> None:
        """Stop a reviewer's review, or the planner's turn, on its own; else the work lane's."""
        if agent.role in REVIEW_ROLES and self._parts.lane is not None:
            self._parts.lane.cancel(agent.task_id)
        elif agent.role == PLANNER:
            self._parts.seat.cancel()
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
