"""A control plane serving many workspaces: its Catalog lists every one, each connection gets
the events of the workspace it's attached to alone, and its commands go there."""

import asyncio
from collections.abc import Sequence

from orbit.agent import Agents, ControlPlane, WorkspaceHost, WorkspaceOwner
from orbit.agent.application.connection import SessionConnection
from orbit.protocol import (
    UNKNOWN,
    WRONG_SESSION,
    AddTask,
    Attach,
    BusyChanged,
    Catalog,
    Command,
    CommandEnvelope,
    CommandRefused,
    Event,
    EventEnvelope,
    RenameSession,
    RunTasks,
    TasksChanged,
    TaskSpec,
    TurnEndReason,
    Welcome,
    WorkspaceEntry,
)
from tests.agent_fakes import HELLO, ROLES, Call, FakeHost, ScriptedLauncher, board_of

TASK = TaskSpec(prompt="Add a flag.")


def workspace(name: str, *, owner: WorkspaceOwner | None = None) -> WorkspaceHost:
    """The host of the workspace ``name``, at ``/src/<name>``, its id ``name``, read-only if
    another ``owner`` owns it."""
    return WorkspaceHost(
        workspace=f"/src/{name}", workspace_id=name, new_id=lambda: f"{name}-1", owner=owner
    )


async def say(connection: SessionConnection, command_id: str, command: Command) -> None:
    await connection.send(CommandEnvelope(command_id=command_id, payload=command))


async def joined(plane: ControlPlane, host: WorkspaceHost) -> SessionConnection:
    """A new connection to ``plane`` that has said Hello and attached to ``host``'s open
    session, named for its workspace."""
    connection = plane.connect()
    name = host.workspace_id
    await say(connection, f"{name}-hello", HELLO)
    attach = Attach(workspace=name, session=host.saved_session.id)
    await say(connection, f"{name}-attach", attach)
    return connection


def taken(connection: SessionConnection) -> list[EventEnvelope]:
    """The envelopes queued for ``connection`` so far."""
    queue = connection._queue
    envelopes: list[EventEnvelope] = []
    while not queue.empty():
        envelopes.append(queue.get_nowait())
    return envelopes


def payloads(envelopes: Sequence[EventEnvelope]) -> list[Event]:
    return [envelope.payload for envelope in envelopes]


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
    in_a, in_b = await joined(plane, first), await joined(plane, second)
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
    in_b = await joined(plane, second)

    await say(in_b, "add", AddTask(task=TASK))
    await plane.close()

    assert board_of(first).tasks == ()
    assert [state.task for state in board_of(second).tasks] == [TASK]


async def test_attaching_to_another_workspace_once_attached_is_refused_naming_both() -> None:
    first, second = workspace("a"), workspace("b")
    plane = ControlPlane([first, second])
    in_a = await joined(plane, first)
    taken(in_a)

    await say(in_a, "attach", Attach(workspace="b", session="b-1"))
    [refusal] = payloads(taken(in_a))
    await plane.close()

    assert refusal == CommandRefused(
        command_id="attach",
        code=WRONG_SESSION,
        message="Workspace 'b' isn't the one attached; this connection is attached to a.",
    )


async def test_a_change_to_one_workspaces_sessions_sends_everyone_the_whole_catalog() -> None:
    first, second = workspace("a"), workspace("b")
    plane = ControlPlane([first, second])
    in_a, in_b = await joined(plane, first), await joined(plane, second)
    taken(in_a), taken(in_b)

    await say(in_a, "rename", RenameSession(session_id="a-1", name="Renamed"))
    [catalog] = [e for e in payloads(taken(in_b)) if isinstance(e, Catalog)]
    await plane.close()

    in_first, in_second = catalog.workspaces
    assert [s.name for s in in_first.sessions] == ["Renamed"]
    assert [s.id for s in in_second.sessions] == ["b-1"]


async def test_every_workspaces_events_share_one_count_of_seqs() -> None:
    first, second = workspace("a"), workspace("b")
    plane = ControlPlane([first, second])
    in_a, in_b = await joined(plane, first), await joined(plane, second)
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


def working(name: str, gate: asyncio.Event, started: list[str]) -> WorkspaceHost:
    """The host of the workspace ``name`` whose task's agent notes it in ``started`` and then
    waits for ``gate`` before giving up."""

    async def waits(call: Call) -> TurnEndReason:
        started.append(name)
        await gate.wait()
        await call("give_up", reason="Done waiting.")
        return "completed"

    agent_host = FakeHost()
    launcher = ScriptedLauncher(agent_host, waits)
    agents = Agents(agent_host, launcher, lambda: f"{name}-token", roles=ROLES, exit_grace=0.5)
    return WorkspaceHost(
        agents=agents, workspace=f"/src/{name}", workspace_id=name, new_id=lambda: f"{name}-1"
    )


async def test_a_task_runs_in_each_workspace_at_once() -> None:
    gate = asyncio.Event()
    started: list[str] = []
    first, second = working("a", gate, started), working("b", gate, started)
    plane = ControlPlane([first, second])
    in_a, in_b = await joined(plane, first), await joined(plane, second)
    for connection in (in_a, in_b):
        await say(connection, "add", AddTask(task=TASK))
        await say(connection, "run", RunTasks())

    async def both_started() -> None:
        while len(started) < 2:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(both_started(), 5)
    gate.set()
    for connection in (in_a, in_b):
        await asyncio.wait_for(idle(connection), 5)
    await plane.close()

    assert sorted(started) == ["a", "b"]


async def idle(connection: SessionConnection) -> None:
    """Wait until the work lane of ``connection``'s workspace goes idle."""
    async for envelope in connection.events():
        event = envelope.payload
        if isinstance(event, BusyChanged) and event.lane == "work" and not event.busy:
            return
