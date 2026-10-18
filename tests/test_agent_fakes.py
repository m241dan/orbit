"""The test helpers in agent_fakes: gathering a session's events up to a given one."""

import pytest

from orbit.protocol import PLANNER, BusyChanged, SendMessage, SettingsChanged, TurnEndReason
from tests.agent_fakes import Call, agent_exited, events_until, idle, send, session_for


async def chats(call: Call) -> TurnEndReason:
    return "completed"


async def test_events_until_fails_naming_the_event_when_it_never_comes() -> None:
    session, _, _ = session_for()
    with pytest.raises(AssertionError, match=r"agent_exited\('nobody'\) within 0.05s"):
        await events_until(session, agent_exited("nobody"), timeout=0.05)


async def test_events_until_stops_at_the_first_done_event_leaving_out_skipped_ones() -> None:
    session, _, launcher = session_for()
    launcher.planner_turns = [chats]
    await send(session, SendMessage(agent=PLANNER, text="hello"))
    events = await events_until(session, idle("planner"), skip=(SettingsChanged,))

    assert events[-1] == BusyChanged(busy=False, lane="planner")
    assert not any(isinstance(e, SettingsChanged) for e in events)
