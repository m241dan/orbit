"""Purging a workspace as it's removed, from a frontend: under the same refusals as removing,
its records are deleted once its host is closed, before its lock is let go, unless that would
lose a clone's work."""

import asyncio

from orbit.agent import ControlPlane, WorkspaceOwner
from orbit.protocol import BUSY, INTERNAL, INVALID, CommandAccepted, RemoveWorkspace, TakeOwnership
from tests.agent_fakes import FakeLock
from tests.connection_helpers import joined, say, taken
from tests.plane_fakes import FakeSource, ids, offered, payloads, refused, welcomed, workspace

PURGE = RemoveWorkspace(workspace="c", purge=True)


async def test_purging_an_idle_workspace_deletes_its_records_before_letting_it_go() -> None:
    first, second = workspace("a"), workspace("c")
    source = FakeSource()
    plane = ControlPlane([first, second], workspaces=source)
    connection = await welcomed(plane)

    await say(connection, "purge", PURGE)
    catalog, accepted = payloads(taken(connection))
    await plane.close()

    assert ids(catalog) == ["a"] and accepted == CommandAccepted(command_id="purge")
    assert source.steps == ["purge c", "close c"] and source.purged == ["c"]


async def test_removing_without_purge_keeps_the_records() -> None:
    source = FakeSource()
    plane = ControlPlane([workspace("c")], workspaces=source)
    connection = await welcomed(plane)

    await say(connection, "remove", RemoveWorkspace(workspace="c"))
    await plane.close()

    assert source.closed == ["c"] and source.purged == []


async def test_a_clone_that_would_lose_work_is_refused_as_invalid_and_stays_served() -> None:
    source = FakeSource()
    source.lost["c"] = ["1 commit on main not on its remote"]
    plane = ControlPlane([workspace("c")], workspaces=source)
    connection = await welcomed(plane)

    await say(connection, "purge", PURGE)
    outcome = payloads(taken(connection))
    served = [host.workspace_id for host in plane.hosts]
    await plane.close()

    message = (
        "Can't purge /src/c: deleting its clone would lose 1 commit on main not on its remote."
    )
    assert outcome == [refused("purge", INVALID, message)]
    assert served == ["c"] and source.flags == [] and source.purged == []


async def test_a_workspace_whose_losses_cant_be_found_out_is_refused_and_stays_served() -> None:
    source = FakeSource()
    source.losses_fail = "The store failed."
    plane = ControlPlane([workspace("c")], workspaces=source)
    connection = await welcomed(plane)

    await say(connection, "purge", PURGE)
    outcome = payloads(taken(connection))
    served = [host.workspace_id for host in plane.hosts]
    await plane.close()

    assert outcome == [refused("purge", INTERNAL, "Can't purge /src/c: The store failed.")]
    assert served == ["c"] and source.flags == []


async def test_a_busy_workspace_is_refused_before_its_losses_are_counted() -> None:
    host = workspace("c")
    source = FakeSource()
    source.losses_fail = "Never asked."
    plane = ControlPlane([host], workspaces=source)
    await joined(plane, host=host)
    connection = await welcomed(plane)

    await say(connection, "purge", PURGE)
    outcome = payloads(taken(connection))
    await plane.close()

    message = "Can't remove /src/c: another frontend views its session c-1."
    assert outcome == [refused("purge", BUSY, message)]


async def test_a_purge_that_fails_leaves_the_workspace_removed_and_says_so() -> None:
    source = FakeSource()
    source.purge_fails = "The disk is full."
    plane = ControlPlane([workspace("c")], workspaces=source)
    connection = await welcomed(plane)

    await say(connection, "purge", PURGE)
    catalog, outcome = payloads(taken(connection))
    await plane.close()

    message = "Removed /src/c, but couldn't purge it: The disk is full."
    assert ids(catalog) == [] and outcome == refused("purge", INTERNAL, message)
    assert source.steps == ["purge c", "close c"] and plane.hosts == ()


async def test_a_workspace_another_orbit_owns_is_refused_purging_as_busy_and_stays_served() -> None:
    source = FakeSource()
    plane = ControlPlane([workspace("c", owner=WorkspaceOwner(pid=42))], workspaces=source)
    connection = await welcomed(plane)

    await say(connection, "purge", PURGE)
    outcome = payloads(taken(connection))
    served = [host.workspace_id for host in plane.hosts]
    await plane.close()

    message = "Can't purge /src/c: an orbit chat, pid 42 owns its repository."
    assert outcome == [refused("purge", BUSY, message)]
    assert served == ["c"] and source.flags == [] and source.steps == []


async def test_a_workspace_another_orbit_owns_can_still_be_removed_without_purge() -> None:
    source = FakeSource()
    plane = ControlPlane([workspace("c", owner=WorkspaceOwner(pid=42))], workspaces=source)
    connection = await welcomed(plane)

    await say(connection, "remove", RemoveWorkspace(workspace="c"))
    catalog, accepted = payloads(taken(connection))
    await plane.close()

    assert ids(catalog) == [] and accepted == CommandAccepted(command_id="remove")
    assert source.steps == ["close c"] and source.purged == []


async def test_a_workspace_taken_over_from_another_orbit_is_purged_as_any_other() -> None:
    host = workspace("c", owner=WorkspaceOwner(pid=42), lock=FakeLock(free=True))
    source = FakeSource()
    plane = ControlPlane([host], workspaces=source)
    connection = await joined(plane, host=host)
    await asyncio.wait_for(offered(connection), 5)
    await say(connection, "take", TakeOwnership(take=True))

    await say(connection, "purge", PURGE)
    outcome = payloads(taken(connection))[-1]
    await plane.close()

    assert outcome == CommandAccepted(command_id="purge")
    assert source.steps == ["purge c", "close c"] and source.purged == ["c"]


async def test_a_purge_asks_what_it_would_lose_once() -> None:
    source = FakeSource()
    plane = ControlPlane([workspace("c")], workspaces=source)
    connection = await welcomed(plane)

    await say(connection, "purge", PURGE)
    await plane.close()

    assert source.losses_asked == 1 and source.purged == ["c"]
