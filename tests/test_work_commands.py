"""Tests for the work commands on their own, in a session without agents."""

import asyncio
from dataclasses import dataclass, field

import pytest

from orbit.agent.application.outbox import EventSink
from orbit.agent.application.planner_seat import PlannerSeat, SeatHooks
from orbit.agent.application.proposals import FrontendReview
from orbit.agent.application.settings import SessionSettings
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.work_commands import CommandParts, WorkCommand, WorkCommands
from orbit.agent.application.work_turns import WorkParts, WorkTurns
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.refusals import Refused
from orbit.protocol import (
    INTERNAL,
    INVALID,
    NOT_CONFIGURED,
    PLANNER,
    SUPER_REVIEW,
    UNKNOWN,
    AddTask,
    BusyChanged,
    CancelAgent,
    CancelTurn,
    DecideProposal,
    EditTask,
    ErrorOccurred,
    Event,
    FireTrigger,
    RemoveTask,
    RunTasks,
    SendMessage,
    Settings,
    SettingsChanged,
    TasksChanged,
    TaskSpec,
    UpdateSettings,
)

COMMAND_ID = "c1"
"""The id every command is handled under."""

TASK = TaskSpec(prompt="Add a flag.", settings=Settings(model="opus"))


@dataclass
class Recorded:
    """What the commands published, and when they saved, in order."""

    effects: list[Event | str] = field(default_factory=list)

    async def put(self, event: Event) -> None:
        self.effects.append(event)

    def put_nowait(self, event: Event) -> None:
        self.effects.append(event)

    async def save(self) -> None:
        self.effects.append("saved")


async def nothing() -> None:
    return None


class ScriptedTurns(WorkTurns):
    """A work lane whose runs take, in turn, the first of ``outcomes``: raising it, or
    succeeding for None."""

    def __init__(self, parts: WorkParts, outcomes: list[BaseException | None]) -> None:
        super().__init__(parts)
        self.outcomes = outcomes

    def check_run_tasks(self) -> None:
        return None

    async def run_tasks(self) -> None:
        outcome = self.outcomes.pop(0)
        if outcome is not None:
            raise outcome


def commands_for(
    recorded: Recorded, outcomes: list[BaseException | None] | None = None
) -> tuple[WorkCommands, Tasks]:
    """The work commands of a session without agents, its effects ``recorded``; given
    ``outcomes``, it's read-only and its runs are scripted by them."""
    events: EventSink = recorded
    tasks = Tasks(TaskBoard(), recorded.put)
    seat = PlannerSeat(None, tasks, events, SeatHooks(recorded.save, lambda: None, tuple))
    work = WorkParts(None, None, tasks, None, None, recorded.put_nowait, lambda: False)
    turns = WorkTurns(work) if outcomes is None else ScriptedTurns(work, outcomes)
    parts = CommandParts(
        SessionSettings(Settings()),
        events,
        tasks,
        seat,
        turns,
        None,
        FrontendReview(recorded.put, str),
        None,
        None,
        recorded.save,
        nothing,
        lambda: outcomes is not None,
    )
    return WorkCommands(parts), tasks


async def refusal(commands: WorkCommands, command: WorkCommand) -> tuple[str, str]:
    """The code and message ``commands`` refuse ``command`` with."""
    with pytest.raises(Refused) as refused:
        await commands.handle(command, COMMAND_ID)
    return refused.value.code, str(refused.value)


async def test_updating_the_settings_publishes_them_then_saves() -> None:
    recorded = Recorded()
    commands, _ = commands_for(recorded)
    settings = Settings(model="haiku")
    await commands.handle(UpdateSettings(settings=settings), COMMAND_ID)
    assert recorded.effects == [SettingsChanged(settings=settings), "saved"]


async def test_only_the_planner_can_be_messaged() -> None:
    commands, _ = commands_for(Recorded())
    code, message = await refusal(commands, SendMessage(text="hi", agent="coder"))
    assert code == UNKNOWN
    assert message.startswith("Agent 'coder' can't be messaged; the agents you can message are")


async def test_a_message_without_a_planner_is_refused_and_not_published_as_sent() -> None:
    recorded = Recorded()
    commands, _ = commands_for(recorded)
    code, _ = await refusal(commands, SendMessage(text="hi", agent=PLANNER))
    assert code == NOT_CONFIGURED
    assert recorded.effects == [BusyChanged(busy=False, lane="planner")]


async def test_cancelling_an_unknown_lane_is_refused() -> None:
    commands, _ = commands_for(Recorded())
    code, message = await refusal(commands, CancelTurn(lane="nowhere"))
    assert code == UNKNOWN
    assert message.startswith("No lane 'nowhere'; the lanes are")


async def test_adding_a_task_publishes_the_board() -> None:
    recorded = Recorded()
    commands, tasks = commands_for(recorded)
    await commands.handle(AddTask(task=TASK), COMMAND_ID)
    assert [task.task for task in tasks.board.tasks] == [TASK]
    assert recorded.effects == [TasksChanged(tasks=tasks.board.tasks)]


async def test_editing_then_removing_a_task_changes_the_board() -> None:
    commands, tasks = commands_for(Recorded())
    await commands.handle(AddTask(task=TASK), COMMAND_ID)
    task_id = tasks.board.tasks[0].id
    edited = TASK.model_copy(update={"prompt": "Add two flags."})
    await commands.handle(EditTask(task_id=task_id, task=edited), COMMAND_ID)
    assert [task.task for task in tasks.board.tasks] == [edited]
    await commands.handle(RemoveTask(task_id=task_id), COMMAND_ID)
    assert tasks.board.tasks == ()


async def test_an_unknown_trigger_is_refused() -> None:
    commands, _ = commands_for(Recorded())
    code, message = await refusal(commands, FireTrigger(trigger="nope"))
    assert code == UNKNOWN
    assert message.startswith("No trigger 'nope'; the triggers are")


async def test_a_trigger_given_input_is_refused() -> None:
    commands, _ = commands_for(Recorded())
    code, message = await refusal(commands, FireTrigger(trigger=SUPER_REVIEW, input={"a": "b"}))
    assert (code, message) == (INVALID, f"The {SUPER_REVIEW} trigger takes no input.")


async def test_a_super_review_without_reviews_is_refused() -> None:
    commands, _ = commands_for(Recorded())
    code, message = await refusal(commands, FireTrigger(trigger=SUPER_REVIEW))
    assert (code, message) == (
        NOT_CONFIGURED,
        "Super reviews need reviews turned on, inside a git repository.",
    )


async def test_cancelling_an_agent_not_running_is_refused() -> None:
    commands, _ = commands_for(Recorded())
    code, message = await refusal(commands, CancelAgent(agent_id="agent-9"))
    assert (code, message) == (UNKNOWN, "No running agent 'agent-9'.")


async def test_deciding_on_no_waiting_proposal_is_refused() -> None:
    recorded = Recorded()
    commands, _ = commands_for(recorded)
    code, message = await refusal(commands, DecideProposal(task_id="7", action="accept"))
    assert (code, message) == (UNKNOWN, "No proposal is waiting for task '7'.")
    assert recorded.effects == []


async def queued_runs(recorded: Recorded, outcomes: list[BaseException | None]) -> WorkCommands:
    """Commands that queued one run per outcome while read-only."""
    commands, _ = commands_for(recorded, outcomes)
    for _ in outcomes:
        await commands.handle(RunTasks(), COMMAND_ID)
    return commands


async def own_queued(recorded: Recorded, outcomes: list[BaseException | None]) -> WorkCommands:
    """Commands that queued one run per outcome while read-only, then owned the repository."""
    commands = await queued_runs(recorded, outcomes)
    await commands.own(COMMAND_ID)
    return commands


async def test_a_queued_command_that_fails_reports_an_error_and_the_rest_still_run() -> None:
    recorded = Recorded()
    outcomes: list[BaseException | None] = [None, RuntimeError("disk gone"), None]
    commands = await own_queued(recorded, outcomes)
    assert outcomes == []
    message = "Orbit failed to handle RunTasks: disk gone"
    assert recorded.effects == [ErrorOccurred(code=INTERNAL, message=message)]
    assert commands.queued == ()


async def test_a_queued_command_refused_reports_its_refusal_and_the_rest_still_run() -> None:
    recorded = Recorded()
    refused = Refused(NOT_CONFIGURED, "No agents.")
    outcomes: list[BaseException | None] = [refused, None]
    commands = await own_queued(recorded, outcomes)
    assert outcomes == []
    assert recorded.effects == [ErrorOccurred(code=NOT_CONFIGURED, message="No agents.")]
    assert commands.queued == ()


async def test_owning_cancelled_midway_keeps_the_commands_not_yet_run_queued() -> None:
    recorded = Recorded()
    outcomes: list[BaseException | None] = [None, asyncio.CancelledError(), None, None]
    commands = await queued_runs(recorded, outcomes)
    with pytest.raises(asyncio.CancelledError):
        await commands.own(COMMAND_ID)
    assert outcomes == [None, None]
    assert commands.queued == (RunTasks(), RunTasks())


async def test_a_queued_command_whose_failure_cant_be_reported_keeps_the_rest_queued() -> None:
    recorded = Recorded()
    outcomes: list[BaseException | None] = [RuntimeError("disk gone"), None]
    commands = await queued_runs(recorded, outcomes)

    async def unreported(event: Event) -> None:
        raise RuntimeError("outbox closed")

    recorded.put = unreported  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="outbox closed"):
        await commands.own(COMMAND_ID)
    assert outcomes == [None]
    assert commands.queued == (RunTasks(),)
