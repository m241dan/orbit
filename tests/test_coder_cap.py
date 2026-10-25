"""The cap on coders running at once across the workspaces a control plane serves: tasks take
turns between workspaces, say where they wait, and always give their slot back."""

import asyncio
from collections.abc import Callable

from orbit.agent import ControlPlane, WorkspaceHost
from orbit.agent.application.connection import SessionConnection
from orbit.protocol import (
    CONFIGURATOR,
    PLANNER,
    REVIEWER,
    Activity,
    AddTask,
    CancelTurn,
    EditTask,
    RemoveTask,
    RunTasks,
    SendMessage,
    SessionStateChanged,
    TaskSpec,
    TurnEndReason,
)
from tests.agent_fakes import (
    Call,
    FakeTree,
    Script,
    board_of,
    chats,
    gives_up,
)
from tests.connection_helpers import joined, say, taken
from tests.plane_fakes import agent_workspace, idle, payloads
from tests.waiting import eventually

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
        await eventually(lambda: self.started == list(names))


def running(host: WorkspaceHost) -> Callable[[], bool]:
    """Whether ``host`` runs tasks now."""
    return lambda: host.activity.tasks


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


def places_in_line(host: WorkspaceHost) -> list[int | None]:
    """Where ``host``'s open session waits in line for a coder each time the line changes from
    now on."""
    slots = host.coder_slots
    changed = slots.changed
    places: list[int | None] = []

    def noted() -> None:
        places.append(host.activity.waiting_for_coder)
        changed()

    slots.changed = noted
    return places


def states(connection: SessionConnection, session: str) -> list[SessionStateChanged]:
    """The states of ``session`` sent to ``connection`` since last taken."""
    return [
        event
        for event in payloads(taken(connection))
        if isinstance(event, SessionStateChanged) and event.session == session
    ]


async def test_workspaces_take_turns_at_a_single_coder() -> None:
    gates = Gates()
    a, _ = agent_workspace("a", gates.script("a1"), gates.script("a2"))
    b, _ = agent_workspace("b", gates.script("b1"))
    plane = capped(1, a, b)
    in_a, in_b = await joined(plane, host=a), await joined(plane, host=b)
    await runs(in_a, task("First."), task("Second."))
    await gates.until_started("a1")
    taken(in_b)
    await runs(in_b, task())
    await eventually(lambda: b.activity.waiting_for_coder == 1)

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
    hosts = [agent_workspace(name, gates.script(name))[0] for name in ("a", "b", "c")]
    plane = capped(2, *hosts)
    for host in hosts:
        await runs(await joined(plane, host=host), task())
        await eventually(running(host))

    await eventually(lambda: hosts[2].activity.waiting_for_coder == 1)
    await gates.until_started("a", "b")  # Their agents start a moment after their slots.
    gates.let("b")
    await gates.until_started("a", "b", "c")
    gates.let("a", "c")
    await eventually(lambda: plane.coders_running == 0)
    await plane.close()


async def test_a_review_starts_while_every_coder_is_busy() -> None:
    gates = Gates()
    a, _ = agent_workspace(
        "a", gates.script("a1"), reviewers=(gates.script("review"),), tree=FakeTree(), review=True
    )
    b, _ = agent_workspace("b", gates.script("b1"))
    plane = capped(1, a, b)
    in_a, in_b = await joined(plane, host=a), await joined(plane, host=b)
    await runs(in_a, task())
    await gates.until_started("a1")
    await runs(in_b, task())
    await eventually(lambda: b.activity.waiting_for_coder == 1)
    gates.let("a1")

    await eventually(lambda: sorted(gates.started) == ["a1", "b1", "review"])
    assert plane.coders_running == 1
    gates.let("b1", "review")
    await asyncio.wait_for(idle(in_b), 5)
    await plane.close()


async def test_a_configurator_takes_a_coders_slot() -> None:
    gates = Gates()
    a, _ = agent_workspace("a", gates.script("configure"))
    b, _ = agent_workspace("b", gates.script("b1"))
    plane = capped(1, a, b)
    in_a, in_b = await joined(plane, host=a), await joined(plane, host=b)
    await runs(in_a, task("Set up the checks.", CONFIGURATOR))
    await gates.until_started("configure")
    await runs(in_b, task())

    await eventually(lambda: b.activity.waiting_for_coder == 1)
    gates.let("configure")
    await gates.until_started("configure", "b1")
    gates.let("b1")
    await asyncio.wait_for(idle(in_b), 5)
    await plane.close()


async def test_a_planner_turn_and_a_review_task_take_no_coders_slot() -> None:
    gates = Gates()
    a, _ = agent_workspace("a", gates.script("a1"))
    b, launcher = agent_workspace("b", reviewers=(gates.script("review"),))
    launcher.planner_turns.append(gates.script("planner"))
    plane = capped(1, a, b)
    in_a, in_b = await joined(plane, host=a), await joined(plane, host=b)
    await runs(in_a, task())
    await gates.until_started("a1")
    await say(in_b, "say", SendMessage(agent=PLANNER, text="Hello."))
    await runs(in_b, task("Review it.", REVIEWER))

    await eventually(lambda: sorted(gates.started) == ["a1", "planner", "review"])
    assert b.activity.waiting_for_coder is None
    gates.let("a1", "planner", "review")
    await asyncio.wait_for(idle(in_b), 5)
    await plane.close()


async def test_cancelling_a_waiting_run_leaves_the_line_and_holds_it() -> None:
    gates = Gates()
    a, _ = agent_workspace("a", gates.script("a1"), gates.script("a2"))
    b, _ = agent_workspace("b", gates.script("b1"))
    plane = capped(1, a, b)
    in_a, in_b = await joined(plane, host=a), await joined(plane, host=b)
    await runs(in_a, task("First."), task("Second."))
    await gates.until_started("a1")
    await runs(in_b, task())
    await eventually(lambda: b.activity.waiting_for_coder == 1)
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


async def test_closing_while_a_run_waits_leaves_no_slot_taken() -> None:
    gates = Gates()
    a, _ = agent_workspace("a", gates.script("a1"))
    b, _ = agent_workspace("b", gates.script("b1"))
    plane = capped(1, a, b)
    in_a, in_b = await joined(plane, host=a), await joined(plane, host=b)
    await runs(in_a, task())
    await gates.until_started("a1")
    await runs(in_b, task())
    await eventually(lambda: b.activity.waiting_for_coder == 1)
    gates.let("a1")
    await plane.close()

    assert plane.coders_running == 0


async def test_a_waiting_run_whose_place_the_plane_drops_ends_without_joining_again() -> None:
    gates = Gates()
    a, _ = agent_workspace("a", gates.script("a1"))
    b, _ = agent_workspace("b", gates.script("b1"))
    plane = capped(1, a, b)
    in_a, in_b = await joined(plane, host=a), await joined(plane, host=b)
    await runs(in_a, task())
    await gates.until_started("a1")
    await runs(in_b, task())
    await eventually(lambda: b.activity.waiting_for_coder == 1)
    places = places_in_line(b)
    b.coder_slots.drop("b")

    await asyncio.wait_for(idle(in_b), 5)
    assert b.coder_slots.waiting == 0
    assert places == [None]
    gates.let("a1")
    await asyncio.wait_for(idle(in_a), 5)
    assert gates.started == ["a1"]
    assert plane.coders_running == 0
    await plane.close()


async def test_every_slot_is_given_back_however_tasks_end() -> None:
    scripts = (gives_up, chats, gives_up, chats)
    a, b = agent_workspace("a", *scripts)[0], agent_workspace("b", *scripts)[0]
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
    a, _ = agent_workspace("a", gates.script("a1"))
    b, _ = agent_workspace("b", gates.script("b1"))
    plane = capped(1, a, b)
    in_a, in_b = await joined(plane, host=a), await joined(plane, host=b)
    await runs(in_a, task())
    await gates.until_started("a1")
    await runs(in_b, task())
    await eventually(lambda: b.activity.waiting_for_coder == 1)
    await say(in_a, "cancel", CancelTurn())

    await gates.until_started("a1", "b1")
    gates.let("b1")
    await asyncio.wait_for(idle(in_b), 5)
    assert plane.coders_running == 0
    await plane.close()


async def test_no_cap_never_waits() -> None:
    gates = Gates()
    hosts = [agent_workspace(name, gates.script(name))[0] for name in ("a", "b", "c")]
    plane = capped(0, *hosts)
    for host in hosts:
        await runs(await joined(plane, host=host), task())

    await eventually(lambda: sorted(gates.started) == ["a", "b", "c"])
    assert plane.coders_running == 3
    gates.let("a", "b", "c")
    await eventually(lambda: plane.coders_running == 0)
    await plane.close()


async def test_removing_the_only_waiting_task_leaves_the_line_at_once() -> None:
    gates = Gates()
    a, _ = agent_workspace("a", gates.script("a1"))
    b, _ = agent_workspace("b", gates.script("b1"))
    plane = capped(1, a, b)
    in_a, in_b = await joined(plane, host=a), await joined(plane, host=b)
    await runs(in_a, task())
    await gates.until_started("a1")
    await runs(in_b, task())
    await eventually(lambda: b.activity.waiting_for_coder == 1)
    await say(in_b, "remove", RemoveTask(task_id="1"))

    await asyncio.wait_for(idle(in_b), 5)
    assert b.activity.waiting_for_coder is None
    assert plane.coders_running == 1
    gates.let("a1")
    await asyncio.wait_for(idle(in_a), 5)
    assert gates.started == ["a1"]
    assert plane.coders_running == 0
    await plane.close()


async def test_a_coder_task_put_ahead_of_a_waiting_one_runs_on_its_turn() -> None:
    gates = Gates()
    a, _ = agent_workspace("a", gates.script("a1"))
    b, _ = agent_workspace("b", gives_up, gates.script("b1-again"), gates.script("b2"))
    c, _ = agent_workspace("c", gates.script("c1"))
    plane = capped(1, a, b, c)
    in_a, in_b, in_c = [await joined(plane, host=host) for host in (a, b, c)]
    await runs(in_b, task("First."))
    await asyncio.wait_for(idle(in_b), 5)
    await runs(in_a, task())
    await gates.until_started("a1")
    await runs(in_b, task("Second."))
    await eventually(lambda: b.activity.waiting_for_coder == 1)
    await runs(in_c, task())
    await eventually(lambda: c.activity.waiting_for_coder == 2)
    await say(in_b, "edit", EditTask(task_id="1", task=task("First, again.")))
    gates.let("a1")

    await gates.until_started("a1", "b1-again")
    gates.let("b1-again")
    await gates.until_started("a1", "b1-again", "c1")
    gates.let("c1", "b2")
    await asyncio.wait_for(idle(in_b), 5)
    assert gates.started == ["a1", "b1-again", "c1", "b2"]
    assert plane.coders_running == 0
    await plane.close()
