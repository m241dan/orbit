"""The cap on coders running at once across the workspaces a control plane serves: tasks take
turns between workspaces, say where they wait, and always give their slot back."""

import asyncio
import itertools
from collections.abc import Callable
from typing import Any

from orbit.agent import Agents, ControlPlane, WorkspaceHost
from orbit.agent.application.connection import SessionConnection
from orbit.protocol import (
    CONFIGURATOR,
    PLANNER,
    REVIEWER,
    Activity,
    AddTask,
    CancelTurn,
    RemoveTask,
    RunTasks,
    SendMessage,
    SessionStateChanged,
    TaskSpec,
    TurnEndReason,
)
from tests.agent_fakes import (
    ROLES,
    Call,
    FakeHost,
    FakeTree,
    Script,
    ScriptedLauncher,
    board_of,
)
from tests.connection_helpers import joined, say, taken
from tests.plane_fakes import idle, payloads

WAITING_FIRST = Activity(kind="waiting_for_coder", position=1, busy=1)


class Gates:
    """Agents' runs across workspaces, each noting its name in ``started`` as it starts, then
    waiting for its own gate before claiming its task done."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self._gates: dict[str, asyncio.Event] = {}

    def script(self, name: str) -> Script:
        """The run named ``name``."""
        gate = self._gates.setdefault(name, asyncio.Event())

        async def play(call: Call) -> TurnEndReason:
            self.started.append(name)
            await gate.wait()
            await call("get_task")
            await call("complete_task", summary="done")
            return "completed"

        return play

    def let(self, *names: str) -> None:
        """Let the runs ``names`` end."""
        for name in names:
            self._gates[name].set()

    async def until_started(self, *names: str) -> None:
        """Wait until the runs that started are ``names``, in order."""
        await until(lambda: self.started == list(names))


async def until(condition: Callable[[], bool]) -> None:
    """Wait until ``condition`` holds; fails if it doesn't within 5 seconds."""

    async def polled() -> None:
        while not condition():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(polled(), 5)


def running(host: WorkspaceHost) -> Callable[[], bool]:
    """Whether ``host`` runs tasks now."""
    return lambda: host.activity.tasks


def workspace(
    name: str, *scripts: Script, reviewers: tuple[Script, ...] = (), **options: Any
) -> tuple[WorkspaceHost, ScriptedLauncher]:
    """The host of the workspace ``name``, whose tasks run through agents playing ``scripts``
    and reviews through ``reviewers``, one per launch; ``options`` go to :class:`Agents`."""
    agent_host = FakeHost()
    launcher = ScriptedLauncher(agent_host, *scripts, reviewers=reviewers)
    tokens = (f"{name}-token-{n}" for n in itertools.count())
    agents = Agents(
        agent_host,
        launcher,
        lambda: next(tokens),
        roles=ROLES,
        exit_grace=0.5,
        root=f"/src/{name}",
        **options,
    )
    host = WorkspaceHost(
        agents=agents, workspace=f"/src/{name}", workspace_id=name, new_id=lambda: f"{name}-1"
    )
    return host, launcher


def capped(limit: int, *hosts: WorkspaceHost) -> ControlPlane:
    """A control plane serving ``hosts``, running at most ``limit`` coders at once."""
    plane = ControlPlane(hosts)
    plane.cap_coders(limit)
    return plane


async def runs(connection: SessionConnection, *tasks: TaskSpec) -> None:
    """Have ``connection``'s workspace queue ``tasks`` and run them."""
    for n, task in enumerate(tasks):
        await say(connection, f"add-{n}", AddTask(task=task))
    await say(connection, "run", RunTasks())


def task(prompt: str = "Add a flag.", role: str = "coder") -> TaskSpec:
    return TaskSpec(prompt=prompt, role=role)


def states(connection: SessionConnection, session: str) -> list[SessionStateChanged]:
    """The states of ``session`` sent to ``connection`` since last taken."""
    return [
        event
        for event in payloads(taken(connection))
        if isinstance(event, SessionStateChanged) and event.session == session
    ]


async def test_workspaces_take_turns_at_a_single_coder() -> None:
    gates = Gates()
    a, _ = workspace("a", gates.script("a1"), gates.script("a2"))
    b, _ = workspace("b", gates.script("b1"))
    plane = capped(1, a, b)
    in_a, in_b = await joined(plane, host=a), await joined(plane, host=b)
    await runs(in_a, task("First."), task("Second."))
    await gates.until_started("a1")
    taken(in_b)
    await runs(in_b, task())
    await until(lambda: b.activity.waiting_for_coder == 1)

    assert WAITING_FIRST in states(in_b, "b-1")[-1].activity
    gates.let("a1")
    await gates.until_started("a1", "b1")
    assert a.activity.waiting_for_coder == 1
    assert WAITING_FIRST not in states(in_b, "b-1")[-1].activity
    gates.let("b1")
    await gates.until_started("a1", "b1", "a2")
    gates.let("a2")
    await asyncio.wait_for(idle(in_a), 5)
    assert plane.coders_running == 0
    await plane.close()


async def test_a_third_workspace_waits_first_in_line_behind_two_coders() -> None:
    gates = Gates()
    hosts = [workspace(name, gates.script(name))[0] for name in ("a", "b", "c")]
    plane = capped(2, *hosts)
    for host in hosts:
        await runs(await joined(plane, host=host), task())
        await until(running(host))

    await until(lambda: hosts[2].activity.waiting_for_coder == 1)
    await gates.until_started("a", "b")  # Their agents start a moment after their slots.
    gates.let("b")
    await gates.until_started("a", "b", "c")
    gates.let("a", "c")
    await until(lambda: plane.coders_running == 0)
    await plane.close()


async def test_a_review_starts_while_every_coder_is_busy() -> None:
    gates = Gates()
    a, _ = workspace(
        "a", gates.script("a1"), reviewers=(gates.script("review"),), tree=FakeTree(), review=True
    )
    b, _ = workspace("b", gates.script("b1"))
    plane = capped(1, a, b)
    in_a, in_b = await joined(plane, host=a), await joined(plane, host=b)
    await runs(in_a, task())
    await gates.until_started("a1")
    await runs(in_b, task())
    await until(lambda: b.activity.waiting_for_coder == 1)
    gates.let("a1")

    await until(lambda: sorted(gates.started) == ["a1", "b1", "review"])
    assert plane.coders_running == 1
    gates.let("b1", "review")
    await asyncio.wait_for(idle(in_b), 5)
    await plane.close()


async def test_a_configurator_takes_a_coders_slot() -> None:
    gates = Gates()
    a, _ = workspace("a", gates.script("configure"))
    b, _ = workspace("b", gates.script("b1"))
    plane = capped(1, a, b)
    in_a, in_b = await joined(plane, host=a), await joined(plane, host=b)
    await runs(in_a, task("Set up the checks.", CONFIGURATOR))
    await gates.until_started("configure")
    await runs(in_b, task())

    await until(lambda: b.activity.waiting_for_coder == 1)
    gates.let("configure")
    await gates.until_started("configure", "b1")
    gates.let("b1")
    await asyncio.wait_for(idle(in_b), 5)
    await plane.close()


async def test_a_planner_turn_and_a_review_task_take_no_coders_slot() -> None:
    gates = Gates()
    a, _ = workspace("a", gates.script("a1"))
    b, launcher = workspace("b", reviewers=(gates.script("review"),))
    launcher.planner_turns.append(gates.script("planner"))
    plane = capped(1, a, b)
    in_a, in_b = await joined(plane, host=a), await joined(plane, host=b)
    await runs(in_a, task())
    await gates.until_started("a1")
    await say(in_b, "say", SendMessage(agent=PLANNER, text="Hello."))
    await runs(in_b, task("Review it.", REVIEWER))

    await until(lambda: sorted(gates.started) == ["a1", "planner", "review"])
    assert b.activity.waiting_for_coder is None
    gates.let("a1", "planner", "review")
    await asyncio.wait_for(idle(in_b), 5)
    await plane.close()


async def test_cancelling_a_waiting_run_leaves_the_line_and_holds_it() -> None:
    gates = Gates()
    a, _ = workspace("a", gates.script("a1"), gates.script("a2"))
    b, _ = workspace("b", gates.script("b1"))
    plane = capped(1, a, b)
    in_a, in_b = await joined(plane, host=a), await joined(plane, host=b)
    await runs(in_a, task("First."), task("Second."))
    await gates.until_started("a1")
    await runs(in_b, task())
    await until(lambda: b.activity.waiting_for_coder == 1)
    await say(in_b, "cancel", CancelTurn())
    await asyncio.wait_for(idle(in_b), 5)

    assert b.activity.waiting_for_coder is None
    gates.let("a1")
    await gates.until_started("a1", "a2")
    gates.let("a2")
    await asyncio.wait_for(idle(in_a), 5)
    assert [state.status for state in board_of(b).tasks] == ["pending"]
    assert gates.started == ["a1", "a2"]
    assert plane.coders_running == 0
    await plane.close()


async def test_a_task_removed_while_it_waits_takes_no_turn() -> None:
    gates = Gates()
    a, _ = workspace("a", gates.script("a1"))
    b, _ = workspace("b", gates.script("b1"))
    plane = capped(1, a, b)
    in_a, in_b = await joined(plane, host=a), await joined(plane, host=b)
    await runs(in_a, task())
    await gates.until_started("a1")
    await runs(in_b, task())
    await until(lambda: b.activity.waiting_for_coder == 1)
    await say(in_b, "remove", RemoveTask(task_id="1"))
    gates.let("a1")

    await asyncio.wait_for(idle(in_b), 5)
    assert gates.started == ["a1"]
    assert plane.coders_running == 0
    await plane.close()


async def test_closing_while_a_run_waits_leaves_no_slot_taken() -> None:
    gates = Gates()
    a, _ = workspace("a", gates.script("a1"))
    b, _ = workspace("b", gates.script("b1"))
    plane = capped(1, a, b)
    in_a, in_b = await joined(plane, host=a), await joined(plane, host=b)
    await runs(in_a, task())
    await gates.until_started("a1")
    await runs(in_b, task())
    await until(lambda: b.activity.waiting_for_coder == 1)
    gates.let("a1")
    await plane.close()

    assert plane.coders_running == 0


async def gives_up(call: Call) -> TurnEndReason:
    await call("get_task")
    await call("give_up", reason="Can't.")
    return "completed"


async def says_nothing(call: Call) -> TurnEndReason:
    return "completed"


async def test_every_slot_is_given_back_however_tasks_end() -> None:
    scripts = (gives_up, says_nothing, gives_up, says_nothing)
    a, b = workspace("a", *scripts)[0], workspace("b", *scripts)[0]
    plane = capped(1, a, b)
    in_a, in_b = await joined(plane, host=a), await joined(plane, host=b)
    for connection in (in_a, in_b):
        await runs(connection, *(task(f"Task {n}.") for n in range(4)))
    for connection in (in_a, in_b):
        await asyncio.wait_for(idle(connection), 5)

    assert {state.status for state in board_of(a).tasks} == {"gave_up", "error"}
    assert plane.coders_running == 0
    await plane.close()


async def test_a_task_cancelled_as_it_runs_gives_its_slot_back() -> None:
    gates = Gates()
    a, _ = workspace("a", gates.script("a1"))
    b, _ = workspace("b", gates.script("b1"))
    plane = capped(1, a, b)
    in_a, in_b = await joined(plane, host=a), await joined(plane, host=b)
    await runs(in_a, task())
    await gates.until_started("a1")
    await runs(in_b, task())
    await until(lambda: b.activity.waiting_for_coder == 1)
    await say(in_a, "cancel", CancelTurn())

    await gates.until_started("a1", "b1")
    gates.let("b1")
    await asyncio.wait_for(idle(in_b), 5)
    assert plane.coders_running == 0
    await plane.close()


async def test_no_cap_never_waits() -> None:
    gates = Gates()
    hosts = [workspace(name, gates.script(name))[0] for name in ("a", "b", "c")]
    plane = capped(0, *hosts)
    for host in hosts:
        await runs(await joined(plane, host=host), task())

    await until(lambda: sorted(gates.started) == ["a", "b", "c"])
    assert plane.coders_running == 3
    gates.let("a", "b", "c")
    await until(lambda: plane.coders_running == 0)
    await plane.close()
