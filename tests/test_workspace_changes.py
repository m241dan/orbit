"""Adding and removing the workspaces a control plane serves, from a frontend: each starts or
stops a workspace's host, leaves the others alone and sends the Catalog; a local orbit refuses
both."""

import asyncio
from collections.abc import Sequence
from dataclasses import replace

from orbit.agent import (
    ControlPlane,
    PathKind,
    Verification,
    WorkspaceHost,
)
from orbit.agent.application.outbox import SEQ_BLOCK
from orbit.agent.domain.workspaces import LOCAL_ONLY
from orbit.protocol import (
    BUSY,
    INTERNAL,
    INVALID,
    UNKNOWN,
    AddTask,
    AddWorkspace,
    Attach,
    Catalog,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    Event,
    EventEnvelope,
    Notice,
    RemoveWorkspace,
    RunTasks,
    Snapshot,
)
from tests.agent_fakes import HELLO, STEP, FakeStore, Steps
from tests.plane_fakes import (
    TASK,
    FakeSource,
    idle,
    ids,
    joined,
    payloads,
    refused,
    say,
    taken,
    welcomed,
    working,
    workspace,
)


class Slow(WorkspaceHost):
    """The host of the workspace ``name`` that waits for ``gate``, if given, before opening its
    latest session, and then fails to if it ``fails``."""

    def __init__(self, name: str, gate: asyncio.Event | None = None, *, fails: bool) -> None:
        super().__init__(workspace=f"/src/{name}", workspace_id=name, new_id=lambda: f"{name}-1")
        self.gate = gate
        self.fails = fails

    async def open_latest(self) -> None:
        if self.gate is not None:
            await self.gate.wait()
        if self.fails:
            raise RuntimeError("The store is gone.")
        await super().open_latest()


class Unclosable(WorkspaceHost):
    """The host of the workspace ``name`` that fails once it has closed."""

    def __init__(self, name: str) -> None:
        super().__init__(workspace=f"/src/{name}", workspace_id=name, new_id=lambda: f"{name}-1")

    async def close(self) -> None:
        await super().close()
        raise OSError("The store won't close.")


class SlowSource(FakeSource):
    """Workspaces whose hosts wait for ``gate``, if given, before opening their latest session,
    and then fail to if they ``fail``."""

    def __init__(self, gate: asyncio.Event | None = None, *, fail: bool = True) -> None:
        super().__init__()
        self.latest_gate = gate
        self.fail = fail

    def make(self, name: str) -> WorkspaceHost:
        return Slow(name, self.latest_gate, fails=self.fail)


async def test_adding_a_workspace_serves_it_and_sends_the_catalog_listing_it() -> None:
    source = FakeSource()
    plane = ControlPlane([workspace("a")], workspaces=source)
    connection = await welcomed(plane)

    await say(connection, "add", AddWorkspace(path="/src/c"))
    catalog, accepted = payloads(taken(connection))
    await plane.close()

    assert ids(catalog) == ["a", "c"]
    assert accepted == CommandAccepted(command_id="add")
    assert source.opened == ["/src/c"]


async def test_a_workspace_added_opens_its_latest_session_and_runs_its_startup_check() -> None:
    steps = Steps(True)

    class Checked(FakeSource):
        def make(self, name: str) -> WorkspaceHost:
            return WorkspaceHost(
                verification=Verification((STEP,), steps),
                check_at_startup=True,
                workspace=f"/src/{name}",
                workspace_id=name,
            )

    plane = ControlPlane(workspaces=Checked())
    connection = await welcomed(plane)
    await say(connection, "add", AddWorkspace(path="/src/c"))

    async def checked() -> None:
        while steps.runs != [STEP.name]:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(checked(), 5)
    await plane.close()


async def test_a_workspace_added_reserves_the_seqs_stamped_while_none_was_served() -> None:
    store = FakeStore()

    class Stored(FakeSource):
        def make(self, name: str) -> WorkspaceHost:
            return WorkspaceHost(workspace=f"/src/{name}", workspace_id=name, store=store)

    plane = ControlPlane(workspaces=Stored())
    connection = await welcomed(plane)
    await say(connection, "add", AddWorkspace(path="/src/c"))
    await plane.close()

    assert store.reservations == [SEQ_BLOCK]


async def test_adding_a_workspace_served_already_is_accepted_changing_nothing() -> None:
    source = FakeSource()
    plane = ControlPlane(workspaces=source)
    connection = await welcomed(plane)
    await say(connection, "add", AddWorkspace(path="/src/c"))
    taken(connection)

    await say(connection, "again", AddWorkspace(path="/src/c"))
    again = payloads(taken(connection))
    await plane.close()

    assert again == [CommandAccepted(command_id="again")]
    assert source.opened == ["/src/c"]


async def test_a_path_inside_a_repository_adds_its_top_level() -> None:
    source = FakeSource(tops={"/src/c/docs": "/src/c"})
    plane = ControlPlane(workspaces=source)
    connection = await welcomed(plane)

    await say(connection, "add", AddWorkspace(path="/src/c/docs"))
    catalog, _ = payloads(taken(connection))
    await plane.close()

    assert source.opened == ["/src/c"]
    assert isinstance(catalog, Catalog) and catalog.workspaces[0].path == "/src/c"


async def test_a_relative_path_is_refused_as_invalid() -> None:
    plane = ControlPlane(workspaces=FakeSource())
    connection = await welcomed(plane)

    await say(connection, "add", AddWorkspace(path="relative"))
    outcome = payloads(taken(connection))
    await plane.close()

    message = "Can't add relative: give the absolute path to a directory."
    assert outcome == [refused("add", INVALID, message)]


async def test_a_path_with_nothing_there_is_refused_as_unknown() -> None:
    plane = ControlPlane(workspaces=FakeSource({"/src/gone": PathKind.MISSING}))
    connection = await welcomed(plane)

    await say(connection, "add", AddWorkspace(path="/src/gone"))
    outcome = payloads(taken(connection))
    await plane.close()

    assert outcome == [refused("add", UNKNOWN, "Can't add /src/gone: there's nothing there.")]


async def test_a_path_to_a_file_is_refused_as_invalid() -> None:
    source = FakeSource({"/src/notes.txt": PathKind.FILE})
    plane = ControlPlane(workspaces=source)
    connection = await welcomed(plane)

    await say(connection, "add", AddWorkspace(path="/src/notes.txt"))
    outcome = payloads(taken(connection))
    await plane.close()

    message = "Can't add /src/notes.txt: it isn't a directory."
    assert outcome == [refused("add", INVALID, message)]
    assert source.opened == []


async def test_a_workspace_that_cant_be_served_is_refused_as_invalid_saying_why() -> None:
    source = FakeSource()
    source.unavailable = "Invalid orbit configuration: no roles."
    plane = ControlPlane(workspaces=source)
    connection = await welcomed(plane)

    await say(connection, "add", AddWorkspace(path="/src/c"))
    outcome = payloads(taken(connection))
    await plane.close()

    message = "Can't add /src/c: Invalid orbit configuration: no roles."
    assert outcome == [refused("add", INVALID, message)]
    assert plane.hosts == ()


async def test_a_workspace_whose_store_fails_partway_is_refused_as_internal_saying_why() -> None:
    source = FakeSource()
    source.unavailable = "The store failed."
    source.unavailable_code = INTERNAL
    plane = ControlPlane(workspaces=source)
    connection = await welcomed(plane)

    await say(connection, "add", AddWorkspace(path="/src/c"))
    outcome = payloads(taken(connection))
    await plane.close()

    assert outcome == [refused("add", INTERNAL, "Can't add /src/c: The store failed.")]
    assert plane.hosts == ()


async def test_a_local_orbit_refuses_adding_and_removing_workspaces() -> None:
    plane = ControlPlane([workspace("a")])
    connection = await welcomed(plane)

    await say(connection, "add", AddWorkspace(path="/src/c"))
    await say(connection, "remove", RemoveWorkspace(workspace="a"))
    outcomes = payloads(taken(connection))
    await plane.close()

    assert outcomes == [refused("add", INVALID, LOCAL_ONLY), refused("remove", INVALID, LOCAL_ONLY)]
    assert [host.workspace_id for host in plane.hosts] == ["a"]


async def test_removing_an_idle_workspace_closes_it_and_leaves_the_others() -> None:
    first, second = workspace("a"), workspace("c")
    source = FakeSource()
    plane = ControlPlane([first, second], workspaces=source)
    in_a = await joined(plane, first)
    connection = await welcomed(plane)
    taken(in_a)

    await say(connection, "remove", RemoveWorkspace(workspace="c"))
    catalog, accepted = payloads(taken(connection))
    seen_in_a = payloads(taken(in_a))
    still_open = not first.closing
    await plane.close()

    assert ids(catalog) == ["a"] and accepted == CommandAccepted(command_id="remove")
    assert [ids(event) for event in seen_in_a] == [["a"]]
    assert source.flags == [("c", False)] and source.closed == ["c"]
    assert second.closing and still_open


async def test_a_workspace_whose_flag_cant_be_cleared_stays_served_and_is_refused() -> None:
    host = workspace("c")
    source = FakeSource()
    source.unflag_fails = "The store failed."
    plane = ControlPlane([host], workspaces=source)
    in_c = await joined(plane, host)
    taken(in_c)

    await say(in_c, "remove", RemoveWorkspace(workspace="c"))
    outcome = payloads(taken(in_c))
    served = [served.workspace_id for served in plane.hosts]
    still_open = not host.closing
    await plane.close()

    message = "Can't remove /src/c, which is still served: The store failed."
    assert outcome == [refused("remove", INTERNAL, message)]
    assert served == ["c"] and still_open and in_c.workspace == "c"
    assert source.closed == []


async def test_a_workspace_viewed_as_its_flag_is_cleared_is_flagged_again_and_refused() -> None:
    host = workspace("c")
    source = FakeSource()
    source.flag_gate = asyncio.Event()
    plane = ControlPlane([host], workspaces=source)
    connection = await welcomed(plane)
    removing = asyncio.create_task(say(connection, "remove", RemoveWorkspace(workspace="c")))
    await asyncio.sleep(0.01)
    viewer = asyncio.create_task(joined(plane, host))
    await asyncio.sleep(0.01)

    source.flag_gate.set()
    await asyncio.gather(removing, viewer)
    outcome = payloads(taken(connection))
    served = [served.workspace_id for served in plane.hosts]
    await plane.close()

    message = "Can't remove /src/c: another frontend views its session c-1."
    assert outcome == [refused("remove", BUSY, message)]
    assert served == ["c"] and source.flags == [("c", False), ("c", True)]
    assert source.closed == []


async def test_removing_a_workspace_not_served_is_refused_as_unknown() -> None:
    plane = ControlPlane([workspace("a")], workspaces=FakeSource())
    connection = await welcomed(plane)

    await say(connection, "remove", RemoveWorkspace(workspace="c"))
    outcome = payloads(taken(connection))
    await plane.close()

    message = "No workspace 'c'; the workspace served is a."
    assert outcome == [refused("remove", UNKNOWN, message)]


async def test_removing_a_workspace_running_a_task_is_refused_as_busy() -> None:
    gate = asyncio.Event()
    started: list[str] = []
    host = working("c", gate, started)
    plane = ControlPlane([host], workspaces=FakeSource())
    in_c = await joined(plane, host)
    await say(in_c, "add", AddTask(task=TASK))
    await say(in_c, "run", RunTasks())

    async def runs() -> None:
        while not started:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(runs(), 5)
    taken(in_c)
    await say(in_c, "remove", RemoveWorkspace(workspace="c"))
    refusals = [e for e in payloads(taken(in_c)) if isinstance(e, CommandRefused)]
    gate.set()
    await asyncio.wait_for(idle(in_c), 5)
    await plane.close()

    assert refusals == [refused("remove", BUSY, "Can't remove /src/c: tasks are running.")]


async def test_removing_a_workspace_another_frontend_views_is_refused_as_busy() -> None:
    host = workspace("c")
    plane = ControlPlane([host], workspaces=FakeSource())
    await joined(plane, host)
    connection = await welcomed(plane)

    await say(connection, "remove", RemoveWorkspace(workspace="c"))
    outcome = payloads(taken(connection))
    served = [served.workspace_id for served in plane.hosts]
    await plane.close()

    message = "Can't remove /src/c: another frontend views its session c-1."
    assert outcome == [refused("remove", BUSY, message)]
    assert served == ["c"]


async def test_removing_the_workspace_attached_to_leaves_the_connection_free_to_attach() -> None:
    first, second = workspace("a"), workspace("c")
    plane = ControlPlane([first, second], workspaces=FakeSource())
    in_c = await joined(plane, second)
    taken(in_c)

    await say(in_c, "remove", RemoveWorkspace(workspace="c"))
    removed = payloads(taken(in_c))
    await say(in_c, "attach", Attach(workspace="a", session="a-1"))
    attached = payloads(taken(in_c))
    await plane.close()

    assert [ids(removed[0]), removed[1:]] == [["a"], [CommandAccepted(command_id="remove")]]
    assert in_c.workspace == "a"
    assert any(isinstance(event, Snapshot) for event in attached)
    assert CommandAccepted(command_id="attach") in attached


async def test_an_attach_after_removing_the_workspace_attached_to_gets_a_snapshot() -> None:
    first, second = workspace("a"), workspace("c")
    stale = EventEnvelope(seq=1, session="a-1", payload=Notice(message="stale"))

    async def missed(since: int) -> Sequence[EventEnvelope] | None:
        return [stale]

    first._greeting = replace(first._greeting, missed=missed)
    plane = ControlPlane([first, second], workspaces=FakeSource())
    connection = plane.connect()
    await say(connection, "hello", HELLO.model_copy(update={"since": 0}))
    await say(connection, "attach-c", Attach(workspace="c", session="c-1"))
    await say(connection, "remove", RemoveWorkspace(workspace="c"))
    taken(connection)
    await say(connection, "attach-a", Attach(workspace="a", session="a-1"))
    attached = payloads(taken(connection))
    await plane.close()

    assert any(isinstance(event, Snapshot) for event in attached)
    assert stale.payload not in attached


async def test_a_workspace_being_added_as_the_control_plane_closes_is_closed_with_it() -> None:
    source = FakeSource()
    source.gate = asyncio.Event()
    plane = ControlPlane(workspaces=source)
    connection = await welcomed(plane)
    adding = asyncio.create_task(say(connection, "add", AddWorkspace(path="/src/c")))
    while not source.opened:
        await asyncio.sleep(0.01)

    closing = asyncio.create_task(plane.close())
    await asyncio.sleep(0.01)
    source.gate.set()
    await asyncio.gather(adding, closing)

    [host] = source.hosts
    assert host.closing


async def test_a_change_once_closed_is_refused() -> None:
    plane = ControlPlane(workspaces=FakeSource())
    connection = await welcomed(plane)
    await plane.close()
    outcomes: list[Event] = []

    async def publish(event: Event) -> None:
        outcomes.append(event)

    envelope = CommandEnvelope(command_id="add", payload=AddWorkspace(path="/src/c"))
    await plane._answer(envelope, connection, publish)

    assert outcomes == [refused("add", INVALID, "Orbit is stopping.")]


async def test_a_workspace_whose_latest_session_fails_to_open_is_let_go() -> None:
    source = SlowSource()
    plane = ControlPlane(workspaces=source)
    connection = await welcomed(plane)

    await say(connection, "add", AddWorkspace(path="/src/c"))
    outcome = payloads(taken(connection))
    served = plane.hosts
    await say(connection, "again", AddWorkspace(path="/src/c"))
    await plane.close()

    message = "Orbit failed to handle AddWorkspace: The store is gone."
    assert outcome == [refused("add", INTERNAL, message)]
    assert served == ()
    assert source.closed == ["c", "c"] and source.flags == [("c", False), ("c", False)]
    assert source.opened == ["/src/c", "/src/c"]
    assert source.hosts[0].closing


async def test_an_add_cancelled_as_its_latest_session_opens_lets_the_workspace_go() -> None:
    gate = asyncio.Event()
    source = SlowSource(gate)
    plane = ControlPlane(workspaces=source)
    connection = await welcomed(plane)
    adding = asyncio.create_task(say(connection, "add", AddWorkspace(path="/src/c")))
    while not source.hosts:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.01)

    adding.cancel()
    await asyncio.gather(adding, return_exceptions=True)
    served = plane.hosts
    await plane.close()

    assert adding.cancelled()
    assert served == ()
    assert source.closed == ["c"] and source.flags == [("c", False)]
    assert source.hosts[0].closing


async def test_a_workspace_added_is_not_served_until_its_latest_session_is_open() -> None:
    gate = asyncio.Event()
    source = SlowSource(gate, fail=False)
    plane = ControlPlane(workspaces=source)
    connection = await welcomed(plane)
    adding = asyncio.create_task(say(connection, "add", AddWorkspace(path="/src/c")))
    while not source.hosts:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.01)

    served = plane.hosts
    gate.set()
    await adding
    added = [host.workspace_id for host in plane.hosts]
    await plane.close()

    assert served == () and added == ["c"]


async def test_a_workspace_that_fails_to_close_is_still_removed_from_the_catalog() -> None:
    first, second = workspace("a"), Unclosable("c")
    source = FakeSource()
    plane = ControlPlane([first, second], workspaces=source)
    in_a = await joined(plane, first)
    connection = await welcomed(plane)
    taken(in_a)

    await say(connection, "remove", RemoveWorkspace(workspace="c"))
    catalog, outcome = payloads(taken(connection))
    seen_in_a = payloads(taken(in_a))
    await plane.close()

    assert ids(catalog) == ["a"]
    message = "Orbit failed to handle RemoveWorkspace: The store won't close."
    assert outcome == refused("remove", INTERNAL, message)
    assert [ids(event) for event in seen_in_a] == [["a"]]
    assert source.closed == ["c"]
