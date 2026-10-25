"""A seat per conversational role: a project's own conversational role, beside the planner,
answers in its own lane, keeps its own conversation and settings across saves and resets, and is
messaged, reset and stopped by name."""

import asyncio

from orbit.agent import ControlPlane, WorkspaceHost
from orbit.agent.application.conversation_seats import ConversationSeats
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.planner_seat import SeatHooks
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.roles import Role
from orbit.protocol import (
    BUSY,
    NOT_CONFIGURED,
    PLANNER,
    UNKNOWN,
    ActivateSession,
    AgentStarted,
    BusyChanged,
    CancelAgent,
    CancelTurn,
    Catalog,
    Command,
    CommandRefused,
    ConversationReset,
    Event,
    NewSession,
    ResetConversation,
    SendMessage,
    Settings,
    SettingsChanged,
    TurnFinished,
    UpdateSettings,
)
from tests.agent_fakes import (
    HELLO,
    ROLES,
    FakeStore,
    ScriptedLauncher,
    Ticks,
    agents_for,
    chats,
    events_until,
    gated_planner,
    hangs,
    idle,
    of_type,
    refused,
    send,
    snapshot_of,
)
from tests.connection_helpers import say, taken
from tests.plane_fakes import payloads
from tests.session_builders import opened
from tests.waiting import eventually

ARCHITECT = "architect"
DESIGN = "design"
"""A project's own conversational role, and the lane it answers in."""

ARCHITECT_ROLE = ROLES[PLANNER].model_copy(update={"instructions": "You design.", "lane": DESIGN})

TALKING = {**ROLES, ARCHITECT: ARCHITECT_ROLE}
"""The built-in roles and the architect."""


def talking(
    store: FakeStore | None = None, roles: dict[str, Role] | None = None
) -> tuple[WorkspaceHost, ScriptedLauncher]:
    """A session whose agents have ``roles``, :data:`TALKING` unless given, saved in ``store``,
    if given, as ``session-1`` then ``session-2`` and so on."""
    agents, launcher = agents_for(roles=TALKING if roles is None else roles)
    ids = iter(f"session-{n}" for n in range(1, 100))
    host = WorkspaceHost(agents=agents, store=store, clock=Ticks(), new_id=lambda: next(ids))
    return host, launcher


async def talk(session: WorkspaceHost, agent: str, lane: str) -> list[Event]:
    """The events from messaging ``agent`` until its ``lane`` goes idle."""
    await send(session, SendMessage(agent=agent, text="Hello."))
    return await events_until(session, idle(lane))


async def refusal_of(session: WorkspaceHost, command: Command) -> Event:
    """What ``command``, sent to ``session``, is refused with."""
    await send(session, command)
    return (await events_until(session, of_type(CommandRefused)))[-1]


async def catalog_of(host: WorkspaceHost) -> Catalog:
    """The catalog a connection to a control plane serving ``host`` gets after its Hello."""
    plane = ControlPlane([host])
    connection = plane.connect()
    await say(connection, "hello", HELLO)
    _, listed = payloads(taken(connection))
    await plane.close()
    assert isinstance(listed, Catalog)
    return listed


async def test_the_architect_answers_in_its_lane_while_the_planner_answers_in_its_own() -> None:
    gate = asyncio.Event()
    session, launcher = talking()
    launcher.planner_turns = [gated_planner(gate), chats]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    await eventually(lambda: launcher.launches, poll=0)
    events = await talk(session, ARCHITECT, DESIGN)
    assert BusyChanged(busy=True, lane="planner") in events
    assert BusyChanged(busy=True, lane=DESIGN) in events
    assert BusyChanged(busy=False, lane="planner") not in events
    started = [e for e in events if isinstance(e, AgentStarted)]
    assert [(e.agent_id, e.role) for e in started] == [("agent-1", PLANNER), ("agent-2", ARCHITECT)]
    assert launcher.launches[-1].instructions == "You design."
    gate.set()
    await events_until(session, idle("planner"))
    await session.close()


async def test_each_keeps_its_own_conversation_across_a_save_and_a_reload() -> None:
    store = FakeStore()
    session, launcher = talking(store)
    launcher.planner_turns = [chats, chats, chats, chats]
    await talk(session, PLANNER, "planner")
    await talk(session, ARCHITECT, DESIGN)
    conversations = {PLANNER: "conversation-agent-1", ARCHITECT: "conversation-agent-2"}
    assert store.last.conversations == conversations
    await opened(session, NewSession())
    await opened(session, ActivateSession(session="session-1"))
    await talk(session, ARCHITECT, DESIGN)
    await talk(session, PLANNER, "planner")
    assert launcher.resumed == [None, None, "conversation-agent-2", "conversation-agent-1"]
    await session.close()


async def test_resetting_the_architect_resets_only_its_conversation() -> None:
    store = FakeStore()
    session, launcher = talking(store)
    launcher.planner_turns = [chats, chats, chats, chats]
    await talk(session, PLANNER, "planner")
    await talk(session, ARCHITECT, DESIGN)
    await send(session, ResetConversation(agent=ARCHITECT))
    await events_until(session, lambda e: e == ConversationReset(agent=ARCHITECT))
    assert store.last.conversations == {PLANNER: "conversation-agent-1"}
    await talk(session, ARCHITECT, DESIGN)
    await talk(session, PLANNER, "planner")
    assert launcher.resumed[2:] == [None, "conversation-agent-1"]
    await session.close()


async def test_the_architects_settings_change_alone_and_its_turns_use_them() -> None:
    store = FakeStore()
    session, launcher = talking(store)
    launcher.planner_turns = [chats, chats]
    await talk(session, PLANNER, "planner")
    architects = Settings(model="haiku", effort="max")
    await send(session, UpdateSettings(role=ARCHITECT, settings=architects))
    changed = await events_until(session, of_type(SettingsChanged))
    assert changed[-1] == SettingsChanged(role=ARCHITECT, settings=architects)
    assert session.settings == Settings()
    assert store.last.role_settings == {PLANNER: Settings(), ARCHITECT: architects}
    await talk(session, ARCHITECT, DESIGN)
    launched = launcher.launches[-1].settings
    assert (launched.model, launched.effort) == ("haiku", "max")
    await session.close()


async def test_settings_for_a_role_that_isnt_conversational_are_refused() -> None:
    session, _ = talking()
    refusal = await refusal_of(session, UpdateSettings(role="coder", settings=Settings()))
    message = "Role 'coder' has no settings of its own; the roles with them are"
    assert refusal == refused(UNKNOWN, f"{message} planner and architect.")
    await session.close()


async def test_messaging_a_role_that_isnt_conversational_lists_those_that_are() -> None:
    session, _ = talking()
    refusal = await refusal_of(session, SendMessage(agent="coder", text="Hi."))
    message = "Agent 'coder' can't be messaged; the agents you can message are"
    assert refusal == refused(UNKNOWN, f"{message} planner and architect.")
    await session.close()


async def test_a_disabled_conversational_role_is_refused_as_not_configured() -> None:
    muted = ARCHITECT_ROLE.model_copy(update={"enabled": False})
    session, _ = talking(roles={**ROLES, ARCHITECT: muted})
    disabled = refused(NOT_CONFIGURED, "architect is disabled in this project's configuration.")
    assert await refusal_of(session, SendMessage(agent=ARCHITECT, text="Hi.")) == disabled
    assert await refusal_of(session, ResetConversation(agent=ARCHITECT)) == disabled
    assert (await snapshot_of(session)).busy == {"work": False, "planner": False, "review": False}
    await session.close()


async def test_resetting_the_architect_while_it_answers_is_refused_naming_it() -> None:
    session, launcher = talking()
    launcher.planner_turns = [hangs]
    await send(session, SendMessage(agent=ARCHITECT, text="Design."))
    await eventually(lambda: launcher.launches, poll=0)
    refusal = await refusal_of(session, ResetConversation(agent=ARCHITECT))
    message = "The architect is answering; press ctrl+x to stop it first."
    assert refusal == refused(BUSY, message)
    await session.close()


async def test_cancelling_the_architects_lane_stops_its_turn_alone() -> None:
    gate = asyncio.Event()
    session, launcher = talking()
    launcher.planner_turns = [gated_planner(gate), hangs]
    await send(session, SendMessage(agent=PLANNER, text="Plan."))
    await eventually(lambda: launcher.launches, poll=0)
    await send(session, SendMessage(agent=ARCHITECT, text="Design."))
    await eventually(lambda: len(launcher.launches) == 2, poll=0)
    await send(session, CancelTurn(lane=DESIGN))
    events = await events_until(session, idle(DESIGN))
    assert any(isinstance(e, TurnFinished) and e.reason == "cancelled" for e in events)
    assert (await snapshot_of(session)).busy["planner"]
    gate.set()
    await events_until(session, idle("planner"))
    await session.close()


async def test_cancelling_the_architects_agent_stops_its_turn() -> None:
    session, launcher = talking()
    launcher.planner_turns = [hangs]
    await send(session, SendMessage(agent=ARCHITECT, text="Design."))
    await eventually(lambda: launcher.launches, poll=0)
    await send(session, CancelAgent(agent_id="agent-1"))
    events = await events_until(session, idle(DESIGN))
    assert any(isinstance(e, TurnFinished) and e.reason == "cancelled" for e in events)
    await session.close()


async def test_the_architects_lane_shows_in_the_snapshot_and_the_catalog() -> None:
    session, _ = talking()
    busy = {"work": False, "planner": False, DESIGN: False, "review": False}
    assert (await snapshot_of(session)).busy == busy
    listed = await catalog_of(session)
    assert [(a.name, a.conversational) for a in listed.agents if a.conversational] == [
        (PLANNER, True),
        (ARCHITECT, True),
    ]
    assert listed.lanes == ("work", "planner", DESIGN, "review")


async def test_the_conversations_of_roles_without_a_seat_are_saved_back() -> None:
    store = FakeStore()
    session, launcher = talking(store, roles=ROLES)
    launcher.planner_turns = [chats]
    await talk(session, PLANNER, "planner")
    record = store.last.model_copy(
        update={"id": "talks", "conversations": {PLANNER: "c1", ARCHITECT: "c2"}}
    )
    store.saved.append(record)
    await opened(session, ActivateSession(session="talks"))
    await send(session, UpdateSettings(settings=Settings(model="opus")))
    await events_until(session, of_type(SettingsChanged))
    assert store.last.id == "talks"
    assert store.last.conversations == {PLANNER: "c1", ARCHITECT: "c2"}
    await session.close()


async def nothing() -> None:
    return None


async def test_roles_sharing_a_lane_keep_it_busy_while_either_answers_and_stop_together() -> None:
    critic = ARCHITECT_ROLE.model_copy(update={"instructions": "You critique."})
    agents, launcher = agents_for(roles={**TALKING, "critic": critic})
    events: asyncio.Queue[Event] = asyncio.Queue()
    hooks = SeatHooks(nothing, lambda: None, tuple)
    tasks = Tasks(TaskBoard(), events.put)
    seats = ConversationSeats(AgentDesk(agents, events.put), tasks, events, hooks)
    assert (seats.names, seats.lanes) == ((PLANNER, ARCHITECT, "critic"), ("planner", DESIGN))
    launcher.planner_turns = [hangs, hangs]
    critiquing = seats.seat("critic", "", "")
    await seats.seat(ARCHITECT, "", "").submit("Design.", Settings())
    await critiquing.submit("Critique.", Settings())
    critiquing.cancel()
    await eventually(lambda: not critiquing.running)
    assert seats.busy() == {"planner": False, DESIGN: True}
    seats.cancel(DESIGN)
    await eventually(lambda: not seats.running)
    assert seats.busy() == {"planner": False, DESIGN: False}
    await seats.close()
