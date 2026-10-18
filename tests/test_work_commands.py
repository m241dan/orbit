"""Tests for the work commands on their own, in a session without agents."""

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
    INVALID,
    NOT_CONFIGURED,
    SUPER_REVIEW,
    UNKNOWN,
    AddTask,
    CancelAgent,
    CancelTurn,
    DecideProposal,
    EditTask,
    Event,
    FireTrigger,
    RemoveTask,
    SendMessage,
    Settings,
    SettingsChanged,
    TasksChanged,
    TaskSpec,
    UpdateSettings,
)

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


def commands_for(recorded: Recorded) -> tuple[WorkCommands, Tasks]:
    """The work commands of a session without agents, its effects ``recorded``."""
    events: EventSink = recorded
    tasks = Tasks(TaskBoard(), recorded.put)
    seat = PlannerSeat(None, tasks, events, SeatHooks(recorded.save, lambda: None, tuple))
    turns = WorkTurns(WorkParts(None, None, tasks, None, None, recorded.put_nowait))
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
    )
    return WorkCommands(parts), tasks


async def refusal(commands: WorkCommands, command: WorkCommand) -> tuple[str, str]:
    """The code and message ``commands`` refuse ``command`` with."""
    with pytest.raises(Refused) as refused:
        await commands.handle(command)
    return refused.value.code, str(refused.value)


async def test_updating_the_settings_publishes_them_then_saves() -> None:
    recorded = Recorded()
    commands, _ = commands_for(recorded)
    settings = Settings(model="haiku")
    await commands.handle(UpdateSettings(settings=settings))
    assert recorded.effects == [SettingsChanged(settings=settings), "saved"]


async def test_only_the_planner_can_be_messaged() -> None:
    commands, _ = commands_for(Recorded())
    code, message = await refusal(commands, SendMessage(text="hi", agent="coder"))
    assert code == UNKNOWN
    assert message.startswith("Agent 'coder' can't be messaged; the agents you can message are")


async def test_cancelling_an_unknown_lane_is_refused() -> None:
    commands, _ = commands_for(Recorded())
    code, message = await refusal(commands, CancelTurn(lane="nowhere"))
    assert code == UNKNOWN
    assert message.startswith("No lane 'nowhere'; the lanes are")


async def test_adding_a_task_publishes_the_board() -> None:
    recorded = Recorded()
    commands, tasks = commands_for(recorded)
    await commands.handle(AddTask(task=TASK))
    assert [task.task for task in tasks.board.tasks] == [TASK]
    assert recorded.effects == [TasksChanged(tasks=tasks.board.tasks)]


async def test_editing_then_removing_a_task_changes_the_board() -> None:
    commands, tasks = commands_for(Recorded())
    await commands.handle(AddTask(task=TASK))
    task_id = tasks.board.tasks[0].id
    edited = TASK.model_copy(update={"prompt": "Add two flags."})
    await commands.handle(EditTask(task_id=task_id, task=edited))
    assert [task.task for task in tasks.board.tasks] == [edited]
    await commands.handle(RemoveTask(task_id=task_id))
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
    commands, _ = commands_for(Recorded())
    code, message = await refusal(commands, DecideProposal(task_id="7", action="accept"))
    assert (code, message) == (UNKNOWN, "No proposal is waiting for task '7'.")
