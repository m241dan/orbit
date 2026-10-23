"""Adding a workspace by URL: the control plane accepts it at once, has the repository cloned in
the background, telling every connection of git's progress and any failure, then serves the
clone, managed, as one added by its path."""

import asyncio

import pytest

from orbit.agent import ControlPlane, WorkspaceHost
from orbit.agent.application.clones import Clones
from orbit.agent.application.connection import SessionConnection
from orbit.agent.domain.clones import clone_served
from orbit.agent.domain.workspaces import LOCAL_ONLY
from orbit.protocol import (
    BUSY,
    GIT_FAILED,
    INTERNAL,
    INVALID,
    AddWorkspace,
    Catalog,
    CommandAccepted,
    CommandEnvelope,
    ErrorOccurred,
    Event,
    EventEnvelope,
    WorkspaceCloning,
)
from tests.connection_helpers import joined, say, taken
from tests.plane_fakes import FakeSource, ids, payloads, refused, welcomed, workspace

URL = "https://github.com/o/proj.git"
PROGRESS = WorkspaceCloning(url=URL, phase="Receiving objects", percent=50)


async def received_until(connection: SessionConnection, kind: type[Event]) -> list[EventEnvelope]:
    """The envelopes queued for ``connection`` up to the first of ``kind``."""
    envelopes: list[EventEnvelope] = []
    while not any(isinstance(envelope.payload, kind) for envelope in envelopes):
        await asyncio.sleep(0.01)
        envelopes += taken(connection)
    return envelopes


async def received_served(connection: SessionConnection) -> list[EventEnvelope]:
    """The envelopes queued for ``connection`` up to the clone's last news, once it's served."""
    envelopes: list[EventEnvelope] = []
    while not any(
        isinstance(envelope.payload, WorkspaceCloning) and envelope.payload.workspace
        for envelope in envelopes
    ):
        await asyncio.sleep(0.01)
        envelopes += taken(connection)
    return envelopes


async def cloning(source: FakeSource) -> None:
    """Wait until ``source`` has started cloning."""
    while not source.cloning:
        await asyncio.sleep(0.01)


async def removed(source: FakeSource) -> None:
    """Wait until ``source`` has removed a clone."""
    while not source.removed:
        await asyncio.sleep(0.01)


async def test_a_url_is_accepted_at_once_then_cloned_and_served_managed() -> None:
    source = FakeSource()
    plane = ControlPlane([workspace("a")], workspaces=source)
    connection = await welcomed(plane)

    await say(connection, "add", AddWorkspace(url=URL))
    accepted = payloads(taken(connection))
    envelopes = await received_served(connection)
    await plane.close()

    assert accepted == [CommandAccepted(command_id="add")]
    progress, catalog, served = envelopes
    assert progress.payload == PROGRESS
    assert (progress.workspace, progress.session) == (None, None)
    assert ids(catalog.payload) == ["a", "proj.git"]
    assert served.payload == clone_served(URL, "proj.git")
    assert (served.workspace, served.session) == (None, None)
    assert source.managed == ["/clones/proj.git"]
    assert source.removed == []


async def test_a_clones_news_reaches_connections_attached_too() -> None:
    host = workspace("a")
    source = FakeSource()
    plane = ControlPlane([host], workspaces=source)
    viewer = await joined(plane, host=host)
    taken(viewer)
    connection = await welcomed(plane)

    await say(connection, "add", AddWorkspace(url=URL))
    seen = payloads(await received_until(viewer, Catalog))
    await plane.close()

    assert seen[0] == PROGRESS
    assert ids(next(event for event in seen if isinstance(event, Catalog))) == ["a", "proj.git"]


async def test_a_clones_news_doesnt_reach_a_connection_before_its_hello() -> None:
    source = FakeSource()
    plane = ControlPlane(workspaces=source)
    stranger = plane.connect()
    connection = await welcomed(plane)

    await say(connection, "add", AddWorkspace(url=URL))
    await received_until(connection, Catalog)
    await plane.close()

    assert taken(stranger) == []


async def test_a_clone_that_fails_is_told_as_git_failed_and_serves_nothing() -> None:
    source = FakeSource()
    source.clone_fails = "Couldn't clone it: not found"
    plane = ControlPlane(workspaces=source)
    connection = await welcomed(plane)

    await say(connection, "add", AddWorkspace(url=URL))
    seen = payloads(await received_until(connection, ErrorOccurred))
    await plane.close()

    assert seen == [
        CommandAccepted(command_id="add"),
        PROGRESS,
        ErrorOccurred(code=GIT_FAILED, message="Couldn't clone it: not found", url=URL),
    ]
    assert source.opened == []


async def test_a_clone_that_cant_be_served_is_told_with_the_refusals_code() -> None:
    source = FakeSource()
    source.unavailable = "Its configuration is invalid."
    plane = ControlPlane(workspaces=source)
    connection = await welcomed(plane)

    await say(connection, "add", AddWorkspace(url=URL))
    [*_, error] = payloads(await received_until(connection, ErrorOccurred))
    await plane.close()

    message = f"Couldn't clone {URL}: Can't add /clones/proj.git: Its configuration is invalid."
    assert error == ErrorOccurred(code=INVALID, message=message, url=URL)
    assert source.removed == ["/clones/proj.git"]


async def test_a_clone_whose_serving_fails_is_told_as_internal() -> None:
    class Broken(WorkspaceHost):
        async def open_latest(self) -> None:
            raise RuntimeError("The store is gone.")

    class BrokenSource(FakeSource):
        def make(self, name: str) -> WorkspaceHost:
            return Broken(workspace=f"/clones/{name}", workspace_id=name)

    source = BrokenSource()
    plane = ControlPlane(workspaces=source)
    connection = await welcomed(plane)

    await say(connection, "add", AddWorkspace(url=URL))
    [*_, error] = payloads(await received_until(connection, ErrorOccurred))
    await plane.close()

    message = f"Couldn't clone {URL}: Orbit failed to serve the clone: The store is gone."
    assert error == ErrorOccurred(code=INTERNAL, message=message, url=URL)
    assert source.flags == [("proj.git", False)]
    assert source.removed == ["/clones/proj.git"]


async def test_a_url_being_cloned_already_is_refused_as_busy() -> None:
    source = FakeSource()
    source.clone_gate = asyncio.Event()
    plane = ControlPlane(workspaces=source)
    connection = await welcomed(plane)

    await say(connection, "first", AddWorkspace(url=URL))
    await cloning(source)
    await say(connection, "second", AddWorkspace(url=URL))
    outcomes = payloads(taken(connection))
    source.clone_gate.set()
    await received_until(connection, Catalog)
    await say(connection, "again", AddWorkspace(url=URL))
    again = payloads(taken(connection))
    await plane.close()

    message = f"Can't clone {URL}: it's being cloned already."
    assert outcomes[-1] == refused("second", BUSY, message)
    assert again == [CommandAccepted(command_id="again")]


async def test_a_url_is_free_again_as_soon_as_its_clone_is_served() -> None:
    clones = Clones(lambda _: None)
    served = asyncio.Event()

    async def serve(_: str) -> str:
        served.set()  # Whoever this wakes, as a catalog would, runs before the clone's task ends.
        return "proj"

    clones.start(URL, FakeSource(), serve)
    await served.wait()
    clones.start(URL, FakeSource(), serve)
    await clones.close()


async def test_other_changes_go_on_while_a_clone_runs() -> None:
    source = FakeSource()
    source.clone_gate = asyncio.Event()
    plane = ControlPlane(workspaces=source)
    connection = await welcomed(plane)

    await say(connection, "clone", AddWorkspace(url=URL))
    await cloning(source)
    await say(connection, "add", AddWorkspace(path="/src/c"))
    catalog, accepted = payloads(taken(connection))[-2:]
    await plane.close()

    assert ids(catalog) == ["c"]
    assert accepted == CommandAccepted(command_id="add")


@pytest.mark.parametrize(
    ("command", "message"),
    [
        (AddWorkspace(), "Give AddWorkspace a path or a URL, one of them."),
        (AddWorkspace(path="/src/c", url=URL), "Give AddWorkspace a path or a URL, one of them."),
        (
            AddWorkspace(url="ftp://x"),
            "Can't clone ftp://x: give an https://, ssh:// or file:// URL, user@host:path or an"
            " absolute path.",
        ),
    ],
)
async def test_an_add_with_both_neither_or_an_unknown_source_is_refused_as_invalid(
    command: AddWorkspace, message: str
) -> None:
    source = FakeSource()
    plane = ControlPlane(workspaces=source)
    connection = await welcomed(plane)

    await say(connection, "add", command)
    outcome = payloads(taken(connection))
    await plane.close()

    assert outcome == [refused("add", INVALID, message)]
    assert source.cloning == [] and source.opened == []


async def test_a_local_orbit_refuses_a_url() -> None:
    plane = ControlPlane([workspace("a")])
    connection = await welcomed(plane)

    await say(connection, "add", AddWorkspace(url=URL))
    outcome = payloads(taken(connection))
    await plane.close()

    assert outcome == [refused("add", INVALID, LOCAL_ONLY)]


async def test_a_url_once_closed_is_refused() -> None:
    source = FakeSource()
    plane = ControlPlane(workspaces=source)
    connection = await welcomed(plane)
    await plane.close()
    outcomes: list[Event] = []

    async def publish(event: Event) -> None:
        outcomes.append(event)

    envelope = CommandEnvelope(command_id="add", payload=AddWorkspace(url=URL))
    await plane._answer(envelope, connection, publish)

    assert outcomes == [refused("add", INVALID, "Orbit is stopping.")]
    assert source.cloning == []


async def test_closing_the_control_plane_stops_a_clone_under_way() -> None:
    source = FakeSource()
    source.clone_gate = asyncio.Event()
    plane = ControlPlane(workspaces=source)
    connection = await welcomed(plane)

    await say(connection, "add", AddWorkspace(url=URL))
    await cloning(source)
    await asyncio.wait_for(plane.close(), 5)

    assert source.opened == []


async def test_closing_while_a_clone_waits_to_be_served_removes_it() -> None:
    source = FakeSource()
    source.gate = asyncio.Event()
    plane = ControlPlane(workspaces=source)
    connection = await welcomed(plane)
    adding = asyncio.create_task(say(connection, "add", AddWorkspace(path="/src/c")))
    while not source.opened:
        await asyncio.sleep(0.01)

    await say(connection, "clone", AddWorkspace(url=URL))
    await received_until(connection, WorkspaceCloning)
    closing = asyncio.create_task(plane.close())
    await asyncio.wait_for(removed(source), 5)
    source.gate.set()
    await asyncio.gather(adding, closing)

    assert source.removed == ["/clones/proj.git"]
    assert source.opened == ["/src/c"]


async def test_a_workspace_added_by_path_that_cant_be_served_isnt_removed() -> None:
    source = FakeSource()
    source.unavailable = "Its configuration is invalid."
    plane = ControlPlane(workspaces=source)
    connection = await welcomed(plane)

    await say(connection, "add", AddWorkspace(path="/src/c"))
    outcome = payloads(taken(connection))
    await plane.close()

    message = "Can't add /src/c: Its configuration is invalid."
    assert outcome == [refused("add", INVALID, message)]
    assert source.removed == []
