"""The planner's seat: its turns, its conversation across switches and resets, and its close."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from orbit.agent import ConfigError, Role
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.planner_seat import (
    NO_PLANNER_MESSAGE,
    NOTHING_TO_RESET,
    PLANNER_BUSY_RESET,
    ROLE_KEPT,
    PlannerSeat,
    SeatHooks,
    SeatParts,
)
from orbit.agent.application.ports import PlannerRoleReader
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.sessions import SavedConversation, runtime_changed_message
from orbit.protocol import (
    BUSY,
    INVALID,
    NOT_CONFIGURED,
    PLANNER,
    STORAGE_FAILED,
    AgentExited,
    BusyChanged,
    ConversationReset,
    ErrorOccurred,
    Event,
    Notice,
    Settings,
    TurnFinished,
)
from tests.agent_fakes import ROLES, ScriptedLauncher, agents_for, chats, hangs
from tests.session_builders import on_claude
from tests.waiting import eventually

HALTED_MESSAGE = "The session is halted."
EXITED = AgentExited(agent_id="agent-1", reason="finished")


@dataclass
class Seated:
    """A seat, the events it publishes, and what it asked of its session."""

    seat: PlannerSeat
    events: asyncio.Queue[Event]
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


@dataclass(frozen=True)
class Exits:
    """How the first agents' exits go as they're published: the first ``replacing`` replace the
    seat's planner, as a switch or reset does, and the first ``failed`` then fail to be
    published."""

    replacing: int = 0
    failed: int = 0


QUIET_EXITS = Exits()
"""Exits that all go as they should."""


def seated(
    roles: Mapping[str, Role] | None = ROLES,
    planner_role: PlannerRoleReader | None = None,
    halts: bool = False,
    exits: Exits = QUIET_EXITS,
    role: str = PLANNER,
) -> Seated:
    """The seat of the planner, or the conversational ``role``, with agents in ``roles``, or none
    if None, whose planner role reads again with ``planner_role``, whose session halts as it's
    saved if ``halts``, and whose first agents' exits go as ``exits`` says."""
    events: asyncio.Queue[Event] = asyncio.Queue()
    agents, launcher = agents_for(roles=roles or {}, planner_role=planner_role)
    replacing, failed = exits.replacing, exits.failed

    async def publish(event: Event) -> None:
        nonlocal failed, replacing
        if isinstance(event, AgentExited) and replacing:
            replacing -= 1
            seat.replace(None)
        if isinstance(event, AgentExited) and failed:
            failed -= 1
            raise RuntimeError(f"Publishing {event.agent_id}'s exit failed.")
        await events.put(event)

    desk = AgentDesk(agents, publish) if roles is not None else None
    calls: list[str] = []

    halted = False

    async def save() -> None:
        nonlocal halted
        calls.append("save")
        halted = halts

    def check() -> None:
        if halted:
            raise Refused(STORAGE_FAILED, HALTED_MESSAGE)

    hooks = SeatHooks(save, lambda: calls.append("use"), lambda: (), check)
    seat = PlannerSeat(SeatParts(desk, Tasks(TaskBoard(), events.put), events, hooks), role)
    return Seated(seat, events, launcher, calls)


async def answered(seated: Seated) -> list[Event]:
    """The events of a turn of the planner on a prompt, which plays ``chats``."""
    seated.launcher.planner_turns = [chats]
    await seated.seat.submit("Plan.", Settings())
    await seated.turn_ended()
    return seated.published()


async def test_submitting_without_a_planner_is_refused_and_frees_the_prompt() -> None:
    seated_ = seated(roles=None)
    with pytest.raises(Refused) as refused:
        await seated_.seat.submit("Plan.", Settings())
    assert (refused.value.code, str(refused.value)) == (NOT_CONFIGURED, NO_PLANNER_MESSAGE)
    assert seated_.published() == [BusyChanged(busy=False, lane="planner")]
    assert seated_.calls == []


async def test_a_planner_role_is_needed_too() -> None:
    seated_ = seated(roles={"coder": ROLES["coder"]})
    with pytest.raises(Refused, match=NO_PLANNER_MESSAGE):
        await seated_.seat.submit("Plan.", Settings())


async def test_a_turn_marks_the_session_used_and_saves_before_and_after() -> None:
    seated_ = seated()
    events = await answered(seated_)
    assert seated_.calls == ["use", "save", "save"]
    assert events[0] == BusyChanged(busy=True, lane="planner")
    assert isinstance(events[-2], TurnFinished)
    assert events[-1] == BusyChanged(busy=False, lane="planner")


async def test_a_submit_whose_save_halts_the_session_is_refused_and_starts_no_turn() -> None:
    seated_ = seated(halts=True)
    seated_.launcher.planner_turns = [chats]
    with pytest.raises(Refused) as refused:
        await seated_.seat.submit("Plan.", Settings())
    assert (refused.value.code, str(refused.value)) == (STORAGE_FAILED, HALTED_MESSAGE)
    assert seated_.seat.turn is None
    assert seated_.launcher.launches == []


async def test_the_conversation_is_known_once_a_turn_starts_it() -> None:
    seated_ = seated()
    assert seated_.seat.conversation is None
    await answered(seated_)
    assert seated_.seat.conversation == on_claude("conversation-agent-1")


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
    await eventually(lambda: seated_.launcher.launches, poll=0)
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
    assert seated_.seat.conversation is None


async def test_a_retired_planner_failing_to_close_leaves_none_of_the_others_open() -> None:
    seated_ = seated(exits=Exits(failed=1))
    await answered(seated_)
    seated_.seat.replace(None)
    await answered(seated_)
    seated_.seat.replace(None)
    with pytest.raises(RuntimeError, match="Publishing agent-2's exit failed"):
        await seated_.seat.close_retired()
    assert seated_.published() == [EXITED]


async def test_a_planner_retired_while_the_retired_close_is_closed_too() -> None:
    seated_ = seated(exits=Exits(replacing=1))
    await answered(seated_)
    seated_.seat.replace(None)
    await answered(seated_)
    await seated_.seat.close_retired()
    assert seated_.published() == [EXITED, AgentExited(agent_id="agent-2", reason="finished")]


async def test_a_retired_planner_failing_to_close_leaves_none_retired_meanwhile_open() -> None:
    seated_ = seated(exits=Exits(replacing=1, failed=1))
    await answered(seated_)
    seated_.seat.replace(None)
    await answered(seated_)
    with pytest.raises(RuntimeError, match="Publishing agent-1's exit failed"):
        await seated_.seat.close_retired()
    assert seated_.published() == [AgentExited(agent_id="agent-2", reason="finished")]


async def test_a_replacing_planner_carries_on_the_conversation_given() -> None:
    seated_ = seated()
    seated_.seat.replace(on_claude("conversation-x"))
    assert seated_.seat.conversation == on_claude("conversation-x")
    await answered(seated_)
    assert seated_.launcher.resumed == ["conversation-x"]


async def test_without_a_planner_the_conversation_given_is_kept_to_save() -> None:
    seated_ = seated(roles=None)
    seated_.seat.replace(on_claude("conversation-x"))
    assert seated_.seat.conversation == on_claude("conversation-x")
    seated_.seat.replace(None)
    assert seated_.seat.conversation is None


ON_KIRO = SavedConversation(runtime="kiro", handle="conversation-k")
"""A conversation saved from another runtime than the planner's role runs on, Claude."""


async def test_a_conversation_from_another_runtime_is_kept_to_save_but_not_carried_on() -> None:
    seated_ = seated()
    seated_.seat.replace(ON_KIRO)
    assert seated_.seat.conversation == ON_KIRO
    seated_.seat.replace(None)
    assert seated_.seat.conversation is None


async def test_a_conversation_from_another_runtime_refuses_turns_and_frees_the_prompt() -> None:
    seated_ = seated()
    seated_.seat.replace(ON_KIRO)
    with pytest.raises(Refused) as refused:
        await seated_.seat.submit("Plan.", Settings())
    message = runtime_changed_message(PLANNER, "kiro", "claude")
    assert (refused.value.code, str(refused.value)) == (INVALID, message)
    assert seated_.published() == [BusyChanged(busy=False, lane="planner")]
    assert (seated_.calls, seated_.launcher.launches, seated_.seat.turn) == ([], [], None)


async def test_resetting_a_conversation_from_another_runtime_lets_the_next_turn_start_one() -> None:
    seated_ = seated()
    seated_.seat.replace(ON_KIRO)
    await seated_.seat.reset()
    assert seated_.published() == [ConversationReset(agent=PLANNER)]
    assert seated_.calls == ["save"]
    assert seated_.seat.conversation is None
    await answered(seated_)
    assert seated_.launcher.resumed == [None]
    assert seated_.seat.conversation == on_claude("conversation-agent-1")


async def test_a_reset_ends_the_conversation_and_saves() -> None:
    seated_ = seated()
    await answered(seated_)
    seated_.calls.clear()
    await seated_.seat.reset()
    assert seated_.published() == [EXITED, ConversationReset(agent=PLANNER)]
    assert seated_.calls == ["save"]
    assert seated_.seat.conversation is None


async def test_a_reset_needs_a_planner() -> None:
    seated_ = seated(roles=None)
    with pytest.raises(Refused) as refused:
        await seated_.seat.reset()
    assert (refused.value.code, str(refused.value)) == (NOT_CONFIGURED, NO_PLANNER_MESSAGE)
    assert seated_.published() == []


async def test_a_reset_is_refused_while_the_planner_answers() -> None:
    seated_ = seated()
    seated_.launcher.planner_turns = [hangs]
    await seated_.seat.submit("Plan.", Settings())
    seated_.published()
    with pytest.raises(Refused) as refused:
        await seated_.seat.reset()
    assert (refused.value.code, str(refused.value)) == (BUSY, PLANNER_BUSY_RESET)
    assert seated_.published() == []
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
        ErrorOccurred(code=INVALID, message=f"{ROLE_KEPT}: bad tools"),
        EXITED,
        ConversationReset(agent=PLANNER),
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


ARCHITECT = "architect"
ARCHITECTS = {**ROLES, ARCHITECT: ROLES[PLANNER].model_copy(update={"lane": "design"})}
"""The roles, with an architect in conversation, answering in its own lane."""


def test_a_seat_runs_its_turns_in_its_roles_lane_or_the_planners_without_one() -> None:
    assert seated(ARCHITECTS, role=ARCHITECT).seat.lane == "design"
    assert seated(roles=None).seat.lane == "planner"


async def test_another_roles_seat_answers_in_its_lane_and_names_itself() -> None:
    seated_ = seated(ARCHITECTS, role=ARCHITECT)
    seated_.launcher.planner_turns = [hangs]
    await seated_.seat.submit("Design.", Settings())
    assert seated_.published()[0] == BusyChanged(busy=True, lane="design")
    with pytest.raises(Refused, match="The architect is already answering"):
        await seated_.seat.submit("More.", Settings())
    with pytest.raises(Refused, match="The architect is answering; press ctrl"):
        await seated_.seat.reset()
    await seated_.seat.stop_turn()


async def test_another_roles_seat_has_nothing_to_reset_before_its_first_turn() -> None:
    seated_ = seated(ARCHITECTS, role=ARCHITECT)
    await seated_.seat.reset()
    assert seated_.published() == [Notice(message="The architect has no conversation to reset.")]


async def test_resetting_another_roles_seat_keeps_its_role_as_it_is() -> None:
    seated_ = seated(ARCHITECTS, planner_role=lambda: ConfigError("bad tools"), role=ARCHITECT)
    await answered(seated_)
    await seated_.seat.reset()
    assert seated_.published() == [EXITED, ConversationReset(agent=ARCHITECT)]


async def test_a_disabled_roles_seat_refuses_its_work_naming_it() -> None:
    muted = ROLES[PLANNER].model_copy(update={"lane": "design", "enabled": False})
    seated_ = seated({**ROLES, ARCHITECT: muted}, role=ARCHITECT)
    with pytest.raises(Refused, match="architect is disabled in this project's configuration"):
        await seated_.seat.submit("Design.", Settings())
    assert seated_.published() == [BusyChanged(busy=False, lane="design")]
