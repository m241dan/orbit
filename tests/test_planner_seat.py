"""The planner's seat: its turns, its conversation across switches and resets, and its close."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field

from orbit.agent import Agents, ConfigError, Role
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.planner_seat import (
    NO_PLANNER_MESSAGE,
    NOTHING_TO_RESET,
    PLANNER_BUSY_RESET,
    ROLE_KEPT,
    PlannerSeat,
    SeatHooks,
)
from orbit.agent.application.ports import PlannerRoleReader
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.board import TaskBoard
from orbit.protocol import (
    AgentExited,
    BusyChanged,
    ErrorOccurred,
    Event,
    Notice,
    PlannerReset,
    Settings,
    TurnFinished,
)
from tests.agent_fakes import ROLES, FakeHost, ScriptedLauncher
from tests.test_planner import chats, hangs

EXITED = AgentExited(agent_id="agent-1", reason="finished")


@dataclass
class Seated:
    """A seat, the events it publishes, and what it asked of its session."""

    seat: PlannerSeat
    events: "asyncio.Queue[Event]"
    launcher: ScriptedLauncher
    calls: list[str] = field(default_factory=list)

    def published(self) -> list[Event]:
        """The events published since last asked."""
        events: list[Event] = []
        while not self.events.empty():
            events.append(self.events.get_nowait())
        return events

    async def turn_ended(self) -> None:
        """Wait for the planner's turn, and the report of its end, to finish."""
        turn = self.seat.turn
        assert turn is not None
        await asyncio.wait({turn})
        await asyncio.sleep(0)


def seated(
    roles: Mapping[str, Role] | None = ROLES, planner_role: PlannerRoleReader | None = None
) -> Seated:
    """A seat with agents in ``roles``, or none if None, whose planner role reads again with
    ``planner_role``."""
    events: asyncio.Queue[Event] = asyncio.Queue()
    host = FakeHost()
    launcher = ScriptedLauncher(host)
    agents = Agents(host, launcher, lambda: "token-1", roles or {}, planner_role=planner_role)
    desk = AgentDesk(agents, events.put) if roles is not None else None
    calls: list[str] = []

    async def save() -> None:
        calls.append("save")

    hooks = SeatHooks(save, lambda: calls.append("use"), lambda: ())
    seat = PlannerSeat(desk, Tasks(TaskBoard(), events.put), events, hooks)
    return Seated(seat, events, launcher, calls)


async def answered(seated: Seated) -> list[Event]:
    """The events of a turn of the planner on a prompt, which plays ``chats``."""
    seated.launcher.planner_turns = [chats]
    await seated.seat.submit("Plan.", Settings())
    await seated.turn_ended()
    return seated.published()


async def test_submitting_without_a_planner_is_refused_and_frees_the_prompt() -> None:
    seated_ = seated(roles=None)
    await seated_.seat.submit("Plan.", Settings())
    assert seated_.published() == [
        ErrorOccurred(message=NO_PLANNER_MESSAGE),
        BusyChanged(busy=False, lane="planner"),
    ]
    assert seated_.calls == []


async def test_a_planner_role_is_needed_too() -> None:
    seated_ = seated(roles={"coder": ROLES["coder"]})
    await seated_.seat.submit("Plan.", Settings())
    assert seated_.published()[0] == ErrorOccurred(message=NO_PLANNER_MESSAGE)


async def test_a_turn_marks_the_session_used_and_saves_before_and_after() -> None:
    seated_ = seated()
    events = await answered(seated_)
    assert seated_.calls == ["use", "save", "save"]
    assert events[0] == BusyChanged(busy=True, lane="planner")
    assert isinstance(events[-2], TurnFinished)
    assert events[-1] == BusyChanged(busy=False, lane="planner")


async def test_the_conversation_is_known_once_a_turn_starts_it() -> None:
    seated_ = seated()
    assert seated_.seat.conversation_id is None
    await answered(seated_)
    assert seated_.seat.conversation_id == "conversation-agent-1"


async def test_a_turn_runs_until_cancelled() -> None:
    seated_ = seated()
    seated_.launcher.planner_turns = [hangs]
    await seated_.seat.submit("Plan.", Settings())
    assert seated_.seat.running
    seated_.seat.cancel()
    await seated_.turn_ended()
    assert not seated_.seat.running


async def test_stopping_the_turn_waits_for_it_to_end() -> None:
    seated_ = seated()
    seated_.launcher.planner_turns = [hangs]
    await seated_.seat.submit("Plan.", Settings())
    while not seated_.launcher.launches:
        await asyncio.sleep(0)
    await seated_.seat.stop_turn()
    turn = seated_.seat.turn
    assert turn is not None and turn.done()


async def test_a_replaced_planner_is_closed_only_once_the_retired_are() -> None:
    seated_ = seated()
    await answered(seated_)
    seated_.seat.replace(None)
    assert seated_.published() == []
    await seated_.seat.close_retired()
    assert seated_.published() == [EXITED]
    assert seated_.seat.conversation_id is None


async def test_a_replacing_planner_carries_on_the_conversation_given() -> None:
    seated_ = seated()
    seated_.seat.replace("conversation-x")
    assert seated_.seat.conversation_id == "conversation-x"
    await answered(seated_)
    assert seated_.launcher.resumed == ["conversation-x"]


async def test_without_a_planner_the_conversation_given_is_kept_to_save() -> None:
    seated_ = seated(roles=None)
    seated_.seat.replace("conversation-x")
    assert seated_.seat.conversation_id == "conversation-x"
    seated_.seat.replace(None)
    assert seated_.seat.conversation_id is None


async def test_a_reset_ends_the_conversation_and_saves() -> None:
    seated_ = seated()
    await answered(seated_)
    seated_.calls.clear()
    await seated_.seat.reset()
    assert seated_.published() == [EXITED, PlannerReset()]
    assert seated_.calls == ["save"]
    assert seated_.seat.conversation_id is None


async def test_a_reset_needs_a_planner() -> None:
    seated_ = seated(roles=None)
    await seated_.seat.reset()
    assert seated_.published() == [ErrorOccurred(message=NO_PLANNER_MESSAGE)]


async def test_a_reset_is_refused_while_the_planner_answers() -> None:
    seated_ = seated()
    seated_.launcher.planner_turns = [hangs]
    await seated_.seat.submit("Plan.", Settings())
    seated_.published()
    await seated_.seat.reset()
    assert seated_.published() == [ErrorOccurred(message=PLANNER_BUSY_RESET)]
    await seated_.seat.stop_turn()


async def test_a_reset_before_the_first_turn_has_nothing_to_reset() -> None:
    seated_ = seated()
    await seated_.seat.reset()
    assert seated_.published() == [Notice(message=NOTHING_TO_RESET)]


async def test_a_reset_with_an_invalid_config_keeps_the_role_and_says_why() -> None:
    seated_ = seated(planner_role=lambda: ConfigError("bad tools"))
    await answered(seated_)
    await seated_.seat.reset()
    assert seated_.published() == [
        ErrorOccurred(message=f"{ROLE_KEPT}: bad tools"),
        EXITED,
        PlannerReset(),
    ]


async def test_closing_ends_the_planners_conversation() -> None:
    seated_ = seated()
    await answered(seated_)
    await seated_.seat.close()
    assert seated_.published() == [EXITED]


async def test_closing_without_a_planner_does_nothing() -> None:
    seated_ = seated(roles=None)
    await seated_.seat.close()
    assert seated_.published() == []
