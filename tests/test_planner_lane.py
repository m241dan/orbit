"""The planner's own lane: its turns run alongside task runs."""

import asyncio

from orbit.agent.application.ports import Reply
from orbit.protocol import (
    BUSY,
    PLANNER,
    TIMED_OUT,
    UNKNOWN,
    AddTask,
    AgentStarted,
    BusyChanged,
    CancelAgent,
    CancelTurn,
    CommandRefused,
    ErrorOccurred,
    Event,
    MessageSent,
    RunTasks,
    SendMessage,
    TaskSpec,
    TurnEndReason,
    TurnFinished,
    TurnStarted,
)
from tests.agent_fakes import (
    COMMAND_ID,
    ROLES,
    Call,
    FakeStore,
    Script,
    board,
    events_until,
    refused,
    send,
    session_for,
)
from tests.test_task_runs import claims_done

PLANNER_IDLE = BusyChanged(busy=False, lane="planner")
WORK_IDLE = BusyChanged(busy=False)


def gated_task(gate: asyncio.Event) -> Script:
    async def waits(call: Call) -> TurnEndReason:
        await call("get_task")
        await gate.wait()
        await call("complete_task", summary="ok")
        return "completed"

    return waits


def gated_planner(gate: asyncio.Event) -> Script:
    async def ponders(call: Call) -> TurnEndReason:
        await gate.wait()
        return "completed"

    return ponders


async def chats(call: Call) -> TurnEndReason:
    return "completed"


def errors(events: list[Event]) -> list[str]:
    return [e.message for e in events if isinstance(e, ErrorOccurred)]


async def test_the_planner_answers_while_a_task_runs() -> None:
    gate = asyncio.Event()
    session, _, launcher = session_for(gated_task(gate))
    launcher.planner_turns = [chats]
    await send(session, AddTask(task=TaskSpec(prompt="one")))
    await send(session, RunTasks())
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    events = await events_until(session, lambda e: e == PLANNER_IDLE)
    assert BusyChanged(busy=True) in events
    assert BusyChanged(busy=True, lane="planner") in events
    assert any(isinstance(e, TurnFinished) and e.reason == "completed" for e in events)
    assert WORK_IDLE not in events and errors(events) == []

    gate.set()
    events = await events_until(session, lambda e: e == WORK_IDLE)
    assert [t.status for t in board(events)] == ["completed"]


async def test_tasks_run_while_the_planner_answers() -> None:
    gate = asyncio.Event()
    session, _, launcher = session_for(claims_done)
    launcher.planner_turns = [gated_planner(gate)]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    await send(session, AddTask(task=TaskSpec(prompt="one")))
    await send(session, RunTasks())
    events = await events_until(session, lambda e: e == WORK_IDLE)
    assert [t.status for t in board(events)] == ["completed"]
    assert PLANNER_IDLE not in events and errors(events) == []

    gate.set()
    await events_until(session, lambda e: e == PLANNER_IDLE)


async def test_the_planner_takes_one_prompt_at_a_time() -> None:
    gate = asyncio.Event()
    session, _, launcher = session_for()
    launcher.planner_turns = [gated_planner(gate)]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    await send(session, SendMessage(agent=PLANNER, text="More."))
    events = await events_until(session, lambda e: isinstance(e, CommandRefused))
    assert events[-1] == refused(BUSY, "The planner is already answering.")
    assert errors(events) == []
    gate.set()
    await events_until(session, lambda e: e == PLANNER_IDLE)
    assert len(launcher.launches) == 1


def sent(events: list[Event]) -> list[MessageSent]:
    return [e for e in events if isinstance(e, MessageSent)]


async def test_an_accepted_message_is_published_before_the_planners_turn_and_logged() -> None:
    store = FakeStore()
    session, _, launcher = session_for(store=store)
    launcher.planner_turns = [chats]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    events = await events_until(session, lambda e: e == PLANNER_IDLE)
    accepted = MessageSent(command_id=COMMAND_ID, agent=PLANNER, text="Plan.")
    assert sent(events) == [accepted]
    turn = next(e for e in events if isinstance(e, TurnStarted))
    assert events.index(accepted) < events.index(turn)
    await session.close()
    assert accepted in [envelope.payload for envelope in store.logged]


async def test_a_message_refused_as_busy_is_not_published_as_sent() -> None:
    gate = asyncio.Event()
    session, _, launcher = session_for()
    launcher.planner_turns = [gated_planner(gate)]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    await send(session, SendMessage(agent=PLANNER, text="More."))
    events = await events_until(session, lambda e: isinstance(e, CommandRefused))
    assert [message.text for message in sent(events)] == ["Plan."]
    gate.set()
    await events_until(session, lambda e: e == PLANNER_IDLE)


async def test_only_the_planner_can_be_messaged() -> None:
    session, _, launcher = session_for()
    launcher.planner_turns = [chats]
    for agent in ("coder", "nobody"):
        await send(session, SendMessage(agent=agent, text="hi"))
        events = await events_until(session, lambda e: isinstance(e, CommandRefused))
        message = f"Agent {agent!r} can't be messaged; the agents you can message are planner."
        assert events[-1] == refused(UNKNOWN, message)
        assert errors(events) == []
        assert BusyChanged(busy=True, lane="planner") not in events
        assert sent(events) == []
    assert launcher.launches == []


async def test_cancelling_the_planner_leaves_the_task_run_running() -> None:
    task_gate, planner_gate = asyncio.Event(), asyncio.Event()
    session, _, launcher = session_for(gated_task(task_gate))
    launcher.planner_turns = [gated_planner(planner_gate)]
    await send(session, AddTask(task=TaskSpec(prompt="one")))
    await send(session, RunTasks())
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    await events_until(session, lambda e: isinstance(e, TurnStarted) and e.turn_id != e.agent_id)
    await send(session, CancelTurn(lane="planner"))
    events = await events_until(session, lambda e: e == PLANNER_IDLE)
    assert any(isinstance(e, TurnFinished) and e.reason == "cancelled" for e in events)
    assert WORK_IDLE not in events

    # Not held: the run goes on, and work a review queues would still start.
    task_gate.set()
    events = await events_until(session, lambda e: e == WORK_IDLE)
    assert [t.status for t in board(events)] == ["completed"]


async def test_cancelling_an_unknown_lane_stops_neither_lane() -> None:
    task_gate, planner_gate = asyncio.Event(), asyncio.Event()
    session, _, launcher = session_for(gated_task(task_gate))
    launcher.planner_turns = [gated_planner(planner_gate)]
    await send(session, AddTask(task=TaskSpec(prompt="one")))
    await send(session, RunTasks())
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    await events_until(session, lambda e: isinstance(e, TurnStarted) and e.turn_id != e.agent_id)
    await send(session, CancelTurn(lane="x"))
    events = await events_until(session, lambda e: isinstance(e, CommandRefused))
    assert events[-1] == refused(UNKNOWN, "No lane 'x'; the lanes are work and planner.")
    assert errors(events) == []

    planner_gate.set()
    events = await events_until(session, lambda e: e == PLANNER_IDLE)
    assert any(isinstance(e, TurnFinished) and e.reason == "completed" for e in events)
    task_gate.set()
    events = await events_until(session, lambda e: e == WORK_IDLE)
    assert [t.status for t in board(events)] == ["completed"]


async def test_cancelling_the_planners_agent_stops_only_its_turn() -> None:
    gate = asyncio.Event()
    session, _, launcher = session_for(gated_task(gate))
    launcher.planner_turns = [gated_planner(asyncio.Event())]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    started = await events_until(session, lambda e: isinstance(e, TurnStarted))
    planner = next(e.agent_id for e in started if isinstance(e, TurnStarted))
    assert planner is not None
    await send(session, AddTask(task=TaskSpec(prompt="one")))
    await send(session, RunTasks())
    await send(session, CancelAgent(agent_id=planner))
    events = await events_until(session, lambda e: e == PLANNER_IDLE)
    assert WORK_IDLE not in events
    gate.set()
    events = await events_until(session, lambda e: e == WORK_IDLE)
    assert [t.status for t in board(events)] == ["completed"]


async def test_a_cancelled_planner_turn_ends_as_the_planners() -> None:
    session, _, launcher = session_for()
    launcher.planner_turns = [gated_planner(asyncio.Event())]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    opened, turn = (await events_until(session, lambda e: isinstance(e, TurnStarted)))[-2:]
    assert isinstance(opened, AgentStarted) and isinstance(turn, TurnStarted)
    await send(session, CancelTurn(lane="planner"))
    events = await events_until(session, lambda e: e == PLANNER_IDLE)
    finished = TurnFinished(turn_id=turn.turn_id, agent_id=opened.agent_id, reason="cancelled")
    assert finished in events


async def test_closing_stops_the_planners_turn() -> None:
    session, _, launcher = session_for()
    launcher.planner_turns = [gated_planner(asyncio.Event())]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    await events_until(session, lambda e: isinstance(e, TurnStarted))
    await session.close()
    events = await events_until(session, lambda e: e == PLANNER_IDLE)
    assert any(isinstance(e, TurnFinished) and e.reason == "cancelled" for e in events)


async def test_the_planner_calling_an_unknown_tool_is_refused() -> None:
    replies: list[Reply] = []

    async def calls_unknown(call: Call) -> TurnEndReason:
        replies.append(await call("no_such_tool"))
        return "completed"

    session, _, launcher = session_for()
    launcher.planner_turns = [calls_unknown]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    await events_until(session, lambda e: e == PLANNER_IDLE)
    assert replies == [Reply("Orbit has no tool 'no_such_tool'.", is_error=True)]


async def hangs(call: Call) -> TurnEndReason:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


def finishes(events: list[Event]) -> list[TurnEndReason]:
    return [e.reason for e in events if isinstance(e, TurnFinished)]


async def test_a_planner_turn_that_runs_out_of_time_frees_the_planners_lane() -> None:
    planner = ROLES[PLANNER].model_copy(update={"timeout_minutes": 0.0001})
    session, _, launcher = session_for(roles={**ROLES, PLANNER: planner})
    launcher.planner_turns = [chats, hangs, chats]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    await events_until(session, lambda e: e == PLANNER_IDLE)
    await send(session, SendMessage(agent=PLANNER, text="More."))
    events = await events_until(session, lambda e: e == PLANNER_IDLE)
    assert [e.code for e in events if isinstance(e, ErrorOccurred)] == [TIMED_OUT]
    assert finishes(events) == ["error"]

    await send(session, SendMessage(agent=PLANNER, text="Again."))
    events = await events_until(session, lambda e: e == PLANNER_IDLE)
    assert finishes(events) == ["completed"] and errors(events) == []
    conversation = "conversation-agent-1"
    assert launcher.resumed == [None, conversation, conversation]
