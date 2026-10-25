"""A control plane serving many workspaces: its Catalog lists every one, each connection gets
the events of the workspace it's attached to alone, and its commands go there."""

import asyncio

from orbit.agent import ControlPlane, Verification, WorkspaceHost, WorkspaceOwner
from orbit.agent.domain.workspaces import ATTACH_FIRST
from orbit.protocol import (
    CODER,
    INVALID,
    PLANNER,
    REVIEWER,
    UNKNOWN,
    WRONG_SESSION,
    AddTask,
    AgentEntry,
    Attach,
    Catalog,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    EventEnvelope,
    RenameSession,
    RunTasks,
    SessionStateChanged,
    Snapshot,
    TakeOwnership,
    TasksChanged,
    Welcome,
    WorkspaceEntry,
)
from tests.agent_fakes import HELLO, ROLES, STEP, FakeLock, Steps, board_of
from tests.connection_helpers import joined, say, taken
from tests.plane_fakes import agent_workspace, idle, offered, payloads, working, workspace
from tests.server_fakes import TASK
from tests.waiting import eventually


async def test_the_catalog_lists_every_workspace_served_each_read_only_if_it_is() -> None:
    plane = ControlPlane([workspace("a"), workspace("b", owner=WorkspaceOwner(pid=1))])
    connection = plane.connect()
    await say(connection, "hello", HELLO)
    welcome, catalog = payloads(taken(connection))
    await plane.close()

    assert isinstance(welcome, Welcome) and isinstance(catalog, Catalog)
    shown = [(w.id, w.label, w.path, w.read_only) for w in catalog.workspaces]
    assert shown == [("a", "a", "/src/a", False), ("b", "b", "/src/b", True)]
    assert [[s.id for s in w.sessions] for w in catalog.workspaces] == [["a-1"], ["b-1"]]


async def test_a_connection_gets_the_events_of_its_own_workspace_alone() -> None:
    first, second = workspace("a"), workspace("b")
    plane = ControlPlane([first, second])
    in_a, in_b = await joined(plane, host=first), await joined(plane, host=second)
    taken(in_a), taken(in_b)

    await say(in_b, "add", AddTask(task=TASK))
    seen_in_a, seen_in_b = taken(in_a), taken(in_b)
    await plane.close()

    assert seen_in_a == []
    assert TasksChanged in [type(event) for event in payloads(seen_in_b)]
    assert {envelope.workspace for envelope in seen_in_b} == {"b"}


async def test_a_command_goes_to_the_workspace_its_connection_is_attached_to() -> None:
    first, second = workspace("a"), workspace("b")
    plane = ControlPlane([first, second])
    in_b = await joined(plane, host=second)

    await say(in_b, "add", AddTask(task=TASK))
    await plane.close()

    assert board_of(first).tasks == ()
    assert [state.task for state in board_of(second).tasks] == [TASK]


async def test_attaching_to_another_workspace_moves_the_connection_there_greeting_it() -> None:
    first, second = workspace("a"), workspace("b")
    plane = ControlPlane([first, second])
    in_a = await joined(plane, host=first)
    taken(in_a)

    await say(in_a, "attach", Attach(workspace="b", session="b-1"))
    moved = taken(in_a)
    await plane.close()

    [snapshot] = [e for e in payloads(moved) if isinstance(e, Snapshot)]
    assert in_a.workspace == "b" and snapshot.session.id == "b-1"
    assert {envelope.workspace for envelope in moved if envelope.workspace} == {"b"}
    assert CommandAccepted(command_id="attach") in payloads(moved)


async def test_a_connection_moved_gets_its_new_workspaces_events_alone() -> None:
    first, second = workspace("a"), workspace("b")
    plane = ControlPlane([first, second])
    moving, staying = await joined(plane, host=first), await joined(plane, host=first)
    await say(moving, "attach", Attach(workspace="b", session="b-1"))
    taken(moving)

    await say(staying, "add-a", AddTask(task=TASK))
    seen_from_a = taken(moving)
    in_b = await joined(plane, host=second)
    await say(in_b, "add-b", AddTask(task=TASK))
    seen_from_b = taken(moving)
    await plane.close()

    assert [e for e in payloads(seen_from_a) if not isinstance(e, Catalog)] == []
    assert TasksChanged in [type(event) for event in payloads(seen_from_b)]
    assert {envelope.workspace for envelope in seen_from_b if envelope.workspace} == {"b"}


async def test_moving_to_another_workspace_drops_the_old_sessions_viewers() -> None:
    first, second = workspace("a"), workspace("b")
    plane = ControlPlane([first, second])
    moving, staying = await joined(plane, host=first), await joined(plane, host=first)
    taken(staying)

    await say(moving, "attach", Attach(workspace="b", session="b-1"))
    seen = payloads(taken(staying))
    await plane.close()

    changes = [e for e in seen if isinstance(e, SessionStateChanged)]
    assert [(e.session, e.viewers) for e in changes] == [("a-1", 1)]


async def test_a_refused_move_leaves_the_connection_attached_to_none() -> None:
    first, second = workspace("a"), workspace("b")
    plane = ControlPlane([first, second])
    in_a = await joined(plane, host=first)
    taken(in_a)

    await say(in_a, "attach", Attach(workspace="b", session="b-9"))
    refused = payloads(taken(in_a))
    await say(in_a, "add", AddTask(task=TASK))
    unattached = payloads(taken(in_a))
    await say(in_a, "again", Attach(workspace="a", session="a-1"))
    attached = payloads(taken(in_a))
    await plane.close()

    assert [(e.command_id, e.code) for e in refused if isinstance(e, CommandRefused)] == [
        ("attach", UNKNOWN)
    ]
    assert unattached == [CommandRefused(command_id="add", code=INVALID, message=ATTACH_FIRST)]
    assert in_a.workspace == "a" and any(isinstance(e, Snapshot) for e in attached)


async def test_another_command_for_another_workspace_is_refused_naming_both() -> None:
    first, second = workspace("a"), workspace("b")
    plane = ControlPlane([first, second])
    in_a = await joined(plane, host=first)
    taken(in_a)

    envelope = CommandEnvelope(command_id="add", workspace="b", payload=AddTask(task=TASK))
    await in_a.send(envelope)
    [refusal] = payloads(taken(in_a))
    await plane.close()

    assert refusal == CommandRefused(
        command_id="add",
        code=WRONG_SESSION,
        message="Workspace 'b' isn't the one attached; this connection is attached to a.",
    )
    assert board_of(second).tasks == ()


async def test_a_change_to_one_workspaces_sessions_sends_everyone_the_whole_catalog() -> None:
    first, second = workspace("a"), workspace("b")
    plane = ControlPlane([first, second])
    in_a, in_b = await joined(plane, host=first), await joined(plane, host=second)
    taken(in_a), taken(in_b)

    await say(in_a, "rename", RenameSession(session_id="a-1", name="Renamed"))
    [catalog] = [e for e in payloads(taken(in_b)) if isinstance(e, Catalog)]
    await plane.close()

    in_first, in_second = catalog.workspaces
    assert [s.name for s in in_first.sessions] == ["Renamed"]
    assert [s.id for s in in_second.sessions] == ["b-1"]


async def test_the_catalogs_agents_carry_the_lane_and_defaults_of_the_first_role_named() -> None:
    audit = {"lane": "audit", "model": "opus", "effort": "high"}
    auditor = ROLES[REVIEWER].model_copy(update=audit)
    first, _ = agent_workspace("a", roles={**ROLES, "auditor": auditor})
    second, _ = agent_workspace("b", roles={**ROLES, "auditor": ROLES[REVIEWER]})
    plane = ControlPlane([first, second])
    connection = plane.connect()
    await say(connection, "hello", HELLO)
    _, catalog = payloads(taken(connection))
    await plane.close()

    assert isinstance(catalog, Catalog)
    agents = {agent.name: agent for agent in catalog.agents}
    assert len(catalog.agents) == len(agents) == len(ROLES) + 1
    assert agents["auditor"] == AgentEntry(
        name="auditor",
        label="auditor",
        conversational=False,
        lane="audit",
        model="opus",
        tool_access="restricted",
        effort="high",
    )
    assert agents[CODER] == AgentEntry(
        name=CODER, label="Coder", conversational=False, lane="work", tool_access="full"
    )
    assert agents[PLANNER] == AgentEntry(
        name=PLANNER, label="Planner", conversational=True, lane="planner"
    )


def read_only_in(envelopes: list[EventEnvelope]) -> list[dict[str, bool]]:
    """Which workspaces are read-only in each Catalog among ``envelopes``."""
    catalogs = [e for e in payloads(envelopes) if isinstance(e, Catalog)]
    return [{w.id: w.read_only for w in catalog.workspaces} for catalog in catalogs]


async def test_taking_over_a_workspace_sends_everyone_a_catalog_saying_so() -> None:
    lock = FakeLock(free=True)
    first, second = workspace("a"), workspace("b", owner=WorkspaceOwner(pid=1), lock=lock)
    plane = ControlPlane([first, second])
    in_a, in_b = await joined(plane, host=first), await joined(plane, host=second)
    await asyncio.wait_for(offered(in_b), 5)
    taken(in_a), taken(in_b)

    await say(in_b, "take", TakeOwnership(take=True))
    seen_in_a, seen_in_b = read_only_in(taken(in_a)), read_only_in(taken(in_b))
    await plane.close()

    assert seen_in_a == seen_in_b == [{"a": False, "b": False}]


async def test_declining_a_workspace_sends_no_catalog() -> None:
    lock = FakeLock(free=True)
    first, second = workspace("a"), workspace("b", owner=WorkspaceOwner(pid=1), lock=lock)
    plane = ControlPlane([first, second])
    in_a, in_b = await joined(plane, host=first), await joined(plane, host=second)
    await asyncio.wait_for(offered(in_b), 5)
    taken(in_a)

    await say(in_b, "decline", TakeOwnership(take=False))
    seen_in_a = read_only_in(taken(in_a))
    await plane.close()

    assert seen_in_a == []


async def test_every_workspaces_events_share_one_count_of_seqs() -> None:
    first, second = workspace("a"), workspace("b")
    plane = ControlPlane([first, second])
    in_a, in_b = await joined(plane, host=first), await joined(plane, host=second)
    await say(in_a, "add", AddTask(task=TASK))
    await say(in_b, "add", AddTask(task=TASK))
    seqs = [envelope.seq for envelope in taken(in_a) + taken(in_b)]
    await plane.close()

    assert len(set(seqs)) == len(seqs)


async def test_a_control_plane_serving_nothing_lists_no_workspace_and_refuses_attaching() -> None:
    plane = ControlPlane()
    connection = plane.connect()
    await say(connection, "hello", HELLO)
    await say(connection, "attach", Attach(workspace="a", session="a-1"))
    welcome, catalog, refusal = payloads(taken(connection))
    await plane.close()

    assert isinstance(welcome, Welcome)
    assert isinstance(catalog, Catalog) and catalog.workspaces == ()
    assert refusal == CommandRefused(
        command_id="attach", code=UNKNOWN, message="No workspace 'a'; none is served."
    )
    assert not plane.read_only and not plane.storage_halted


async def test_the_control_plane_is_read_only_while_any_workspace_is() -> None:
    plane = ControlPlane([workspace("a"), workspace("b", owner=WorkspaceOwner(pid=1))])
    await plane.close()
    assert plane.read_only


async def test_closing_ends_the_stream_of_a_connection_attached_to_no_workspace() -> None:
    plane = ControlPlane([workspace("a")])
    connection = plane.connect()
    await say(connection, "hello", HELLO)
    await plane.close()

    assert [type(e.payload) async for e in connection.events()] == [Welcome, Catalog]


async def test_a_host_no_control_plane_serves_tells_no_one_of_its_sessions() -> None:
    host = workspace("a")
    entry = await host.entry()
    await host.catalog_changed(entry)
    await host.close()

    assert entry == WorkspaceEntry(id="a", label="a", path="/src/a", sessions=entry.sessions)


async def test_a_task_runs_in_each_workspace_at_once() -> None:
    gate = asyncio.Event()
    started: list[str] = []
    first, second = working("a", gate, started), working("b", gate, started)
    plane = ControlPlane([first, second])
    in_a, in_b = await joined(plane, host=first), await joined(plane, host=second)
    for connection in (in_a, in_b):
        await say(connection, "add", AddTask(task=TASK))
        await say(connection, "run", RunTasks())
    await eventually(lambda: len(started) >= 2)
    gate.set()
    for connection in (in_a, in_b):
        await asyncio.wait_for(idle(connection), 5)
    await plane.close()

    assert sorted(started) == ["a", "b"]


async def test_a_workspace_served_at_start_runs_its_startup_check_with_no_frontend() -> None:
    steps = Steps(True)
    host = WorkspaceHost(
        verification=Verification((STEP,), steps),
        check_at_startup=True,
        workspace="/src/a",
        workspace_id="a",
    )
    plane = ControlPlane([host])
    await plane.start()
    await eventually(lambda: steps.runs == [STEP.name])
    await plane.close()
