"""The control plane's live gauges, read through an in-memory metric reader as metrics are
collected: the agents running by role and workspace, the tasks waiting for a coder's slot and
those pending in each workspace's open session, the frontends connected, and the bytes each
workspace's sessions' events take in the store."""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from orbit.agent import ControlPlane, WorkspaceHost
from orbit.agent.application.connection import SessionConnection
from orbit.agent.external.sqlite_store import SqliteStore
from orbit.agent.external.workspace_registry import WorkspaceRegistry
from orbit.protocol import (
    AddTask,
    AgentExited,
    AgentStarted,
    EventEnvelope,
    Notice,
    RunTasks,
    TaskSpec,
    TurnEndReason,
)
from tests.agent_fakes import ROLES, Call, Script, Ticks, board_of, notice
from tests.connection_helpers import joined, say
from tests.plane_fakes import agent_workspace, idle, welcomed
from tests.store_views import database
from tests.telemetry_helpers import GaugePoint, Traced
from tests.waiting import eventually

AGENTS = "orbit.agents.active"
QUEUE = "orbit.queue.depth"
PENDING = "orbit.tasks.pending"
FRONTENDS = "orbit.frontends.connected"
LOG_SIZE = "orbit.event_log.size"


@pytest.fixture
def traced() -> Iterator[Traced]:
    """Telemetry recording metrics in memory, closed after the test."""
    traced = Traced()
    yield traced
    traced.telemetry.close()


def gauged(traced: Traced, *hosts: WorkspaceHost) -> ControlPlane:
    """A control plane serving ``hosts``, registering its gauges with ``traced``'s."""
    return ControlPlane(hosts, gauges=traced.telemetry.gauges)


def by_workspace_and_role(points: list[GaugePoint]) -> dict[tuple[object, object], float]:
    """Each of ``points``' values, by its workspace and role."""
    return {
        (attributes["orbit.workspace.id"], attributes["orbit.role"]): value
        for attributes, value in points
    }


def none_running(*workspaces: str) -> dict[tuple[object, object], float]:
    """0 for each built-in role in each of ``workspaces``."""
    return {(workspace, role): 0 for workspace in workspaces for role in ROLES}


def in_workspace(workspace: str) -> dict[str, object]:
    """The attributes of a point of the workspace ``workspace``."""
    return {"orbit.workspace.id": workspace}


async def started(host: WorkspaceHost, agent_id: str, role: str) -> None:
    """Have ``host`` publish that agent ``agent_id`` started in ``role``."""
    await host.outbox.put(AgentStarted(agent_id=agent_id, role=role, task_id=f"{agent_id}-task"))


async def test_agents_active_counts_the_agents_running_in_each_workspace_by_role(
    traced: Traced,
) -> None:
    a, b = agent_workspace("a")[0], agent_workspace("b")[0]
    plane = gauged(traced, a, b)
    await joined(plane, host=b)
    await started(b, "agent-1", "coder")
    await started(b, "agent-2", "reviewer")

    assert by_workspace_and_role(traced.gauge(AGENTS)) == none_running("a", "b") | {
        ("b", "coder"): 1,
        ("b", "reviewer"): 1,
    }
    await plane.close()


async def test_an_agent_that_exits_no_longer_counts(traced: Traced) -> None:
    a = agent_workspace("a")[0]
    plane = gauged(traced, a)
    await joined(plane, host=a)
    await started(a, "agent-1", "coder")
    await started(a, "agent-2", "coder")

    await a.outbox.put(AgentExited(agent_id="agent-1", reason="finished"))

    assert by_workspace_and_role(traced.gauge(AGENTS))["a", "coder"] == 1
    await plane.close()


async def test_an_agent_of_a_role_the_workspace_doesnt_configure_counts_while_it_runs(
    traced: Traced,
) -> None:
    a = agent_workspace("a")[0]
    plane = gauged(traced, a)
    await joined(plane, host=a)
    await started(a, "startup-check", "checker")

    assert by_workspace_and_role(traced.gauge(AGENTS))["a", "checker"] == 1
    await a.outbox.put(AgentExited(agent_id="startup-check", reason="finished"))
    assert ("a", "checker") not in by_workspace_and_role(traced.gauge(AGENTS))
    await plane.close()


async def test_a_workspace_without_agents_has_none_active(traced: Traced) -> None:
    plane = gauged(traced, WorkspaceHost(workspace_id="a"))
    assert traced.gauge(AGENTS) == []
    await plane.close()


class Gate:
    """Agents' runs that wait for the gate to open before claiming their tasks done."""

    def __init__(self) -> None:
        self.opened = asyncio.Event()
        self.started = 0

    def script(self) -> Script:
        async def play(call: Call) -> TurnEndReason:
            self.started += 1
            await self.opened.wait()
            await call("get_task")
            await call("complete_task", summary="done")
            return "completed"

        return play


async def runs(connection: SessionConnection, tasks: int) -> None:
    """Have ``connection``'s workspace queue ``tasks`` tasks and run them."""
    for n in range(tasks):
        await say(connection, f"add-{n}", AddTask(task=TaskSpec(prompt=f"Task {n}.")))
    await say(connection, "run", RunTasks())


class Queued:
    """Three workspaces sharing a single coder: ``a`` runs the first of its two tasks, while
    ``b`` and ``c`` each wait in line for the coder with a task of their own."""

    def __init__(self, traced: Traced) -> None:
        self.gate = Gate()
        self.hosts = [agent_workspace(name, *self._scripts(name))[0] for name in "abc"]
        self.plane = gauged(traced, *self.hosts)
        self.plane.cap_coders(1)
        self.connections: list[SessionConnection] = []

    def _scripts(self, name: str) -> list[Script]:
        return [self.gate.script() for _ in range(2 if name == "a" else 1)]

    async def start(self) -> None:
        """Have each workspace run its tasks, ``a`` first, waiting till the others wait."""
        _, b, c = self.hosts
        self.connections = [await joined(self.plane, host=host) for host in self.hosts]
        await runs(self.connections[0], 2)
        await eventually(lambda: self.gate.started == 1)
        for connection in self.connections[1:]:
            await runs(connection, 1)
        await eventually(lambda: b.activity.waiting_for_coder and c.activity.waiting_for_coder)

    async def close(self) -> None:
        """Let every run end, then close the control plane."""
        self.gate.opened.set()
        for connection in self.connections:
            await asyncio.wait_for(idle(connection), 5)
        await self.plane.close()


async def test_the_queue_depth_is_how_many_tasks_wait_for_a_coders_slot(traced: Traced) -> None:
    queued = Queued(traced)
    await queued.start()

    assert traced.gauge(QUEUE) == [({}, 2)]
    await queued.close()
    assert traced.gauge(QUEUE) == [({}, 0)]


async def test_the_pending_tasks_of_each_workspace_are_its_boards(traced: Traced) -> None:
    queued = Queued(traced)
    await queued.start()

    boards = [board_of(host).tasks for host in queued.hosts]
    pending = [sum(task.status == "pending" for task in tasks) for tasks in boards]
    assert traced.gauge(PENDING) == [
        (in_workspace(name), count) for name, count in zip("abc", pending, strict=True)
    ]
    assert pending == [1, 1, 1]
    await queued.close()


async def test_frontends_connected_counts_the_connections_welcomed(traced: Traced) -> None:
    a = agent_workspace("a")[0]
    plane = gauged(traced, a)
    plane.connect()  # It never says Hello.
    lobby = await welcomed(plane)
    await joined(plane, host=a)

    assert traced.gauge(FRONTENDS) == [({}, 2)]
    await lobby.close()
    assert traced.gauge(FRONTENDS) == [({}, 1)]
    await plane.close()


async def stored_host(
    store: SqliteStore, path: Path, earlier: list[EventEnvelope]
) -> WorkspaceHost:
    """The host of the workspace at ``path``, registered in ``store``, whose earlier session
    ``s0`` logged ``earlier``."""
    path.mkdir()
    registered = await WorkspaceRegistry(store).find_or_register(str(path))
    view = store.workspace_view(registered.workspace_id)
    await view.append("s0", earlier)
    return WorkspaceHost(
        store=view, workspace_id=view.workspace_id, clock=Ticks(), new_id=lambda: "s1"
    )


def stored_sizes(state: Path) -> dict[str, int]:
    """The bytes the logs of each workspace's sessions hold, by its id."""
    with database(state) as connection:
        rows = connection.execute("SELECT workspace_id, sum(size) FROM logs GROUP BY workspace_id")
        return {workspace: size for workspace, size in rows}


async def test_the_event_log_size_of_each_workspace_is_its_sessions_stored_sizes(
    traced: Traced, store: SqliteStore, state: Path, tmp_path: Path
) -> None:
    a = await stored_host(store, tmp_path / "a", [notice(1, "s0"), notice(2, "s0")])
    b = await stored_host(store, tmp_path / "b", [])
    plane = gauged(traced, a, b)
    await plane.start()
    await a.outbox.put(Notice(message="a" * 1000))
    await b.outbox.put(Notice(message="b" * 3000))

    def sizes() -> list[GaugePoint]:
        stored = stored_sizes(state)
        return [(in_workspace(h.workspace_id), stored.get(h.workspace_id, 0)) for h in (a, b)]

    await eventually(lambda: all(size > 1000 for _, size in sizes()))  # Both notices logged.
    await eventually(lambda: traced.gauge(LOG_SIZE) == sizes())
    await plane.close()


async def test_a_workspace_without_a_store_has_an_event_log_size_of_0(traced: Traced) -> None:
    plane = gauged(traced, WorkspaceHost(workspace_id="a"))
    assert traced.gauge(LOG_SIZE) == [(in_workspace("a"), 0)]
    await plane.close()
