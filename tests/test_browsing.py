"""Browsing the server host's directories from a frontend: ``ListDirectory`` answered to the
connection that asked alone, refused as the path calls for, confined to the browsable root, as
``AddWorkspace`` is too; a local orbit refuses it."""

import asyncio
from pathlib import Path

from orbit.agent import Browsing, ControlPlane, DirectoryLister, WorkspaceHost
from orbit.agent.domain.listings import LOCAL_BROWSING, FoundDirectory
from orbit.protocol import (
    INVALID,
    UNKNOWN,
    AddWorkspace,
    CommandAccepted,
    DirectoryEntry,
    DirectoryListing,
    Event,
    ListDirectory,
)
from tests.plane_fakes import (
    FakeSource,
    joined,
    payloads,
    refused,
    say,
    taken,
    welcomed,
    workspace,
)


def browsing(home: Path, root: Path | None = None) -> Browsing:
    """Browsing the real filesystem from ``home``, confined to ``root`` if given."""
    return Browsing(DirectoryLister(), str(home), None if root is None else str(root))


async def listed(plane: ControlPlane, path: str | None) -> list[Event]:
    """What a new connection to ``plane`` that said Hello gets for listing ``path``."""
    connection = await welcomed(plane)
    await say(connection, "list", ListDirectory(path=path))
    return payloads(taken(connection))


def tree(root: Path) -> None:
    """``b``, a repository, ``a``, ``.hidden``, a file and ``l``, a symlink to ``a``, in
    ``root``."""
    (root / "b" / ".git").mkdir(parents=True)
    (root / "a").mkdir()
    (root / ".hidden").mkdir()
    (root / "notes.txt").write_text("")
    (root / "l").symlink_to("a")


async def test_a_directory_lists_its_directories_repositories_first(tmp_path: Path) -> None:
    tree(tmp_path)
    plane = ControlPlane(workspaces=FakeSource(), browsing=browsing(tmp_path))

    outcome = await listed(plane, str(tmp_path))
    await plane.close()

    entries = (
        DirectoryEntry(name="b", is_repository=True, hidden=False),
        DirectoryEntry(name="a", is_repository=False, hidden=False),
        DirectoryEntry(name=".hidden", is_repository=False, hidden=True),
        DirectoryEntry(name="l", is_repository=False, hidden=False, target="a"),
    )
    listing = DirectoryListing(
        path=str(tmp_path), parent=str(tmp_path.parent), entries=entries, total=4
    )
    assert outcome == [listing, CommandAccepted(command_id="list")]


async def test_a_served_workspaces_directory_carries_its_id(tmp_path: Path) -> None:
    (tmp_path / "src" / "a").mkdir(parents=True)

    class There(FakeSource):
        def make(self, name: str) -> WorkspaceHost:
            return WorkspaceHost(workspace=str(tmp_path / "src" / name), workspace_id=name)

    plane = ControlPlane(workspaces=There(), browsing=browsing(tmp_path))
    connection = await welcomed(plane)
    await say(connection, "add", AddWorkspace(path=str(tmp_path / "src" / "a")))
    taken(connection)

    outcome = await listed(plane, str(tmp_path / "src"))
    await plane.close()

    listing = outcome[0]
    assert isinstance(listing, DirectoryListing)
    assert listing.entries == (
        DirectoryEntry(name="a", is_repository=False, workspace="a", hidden=False),
    )


async def test_more_directories_than_the_cap_are_truncated_counting_them_all(
    tmp_path: Path,
) -> None:
    for index in range(2001):
        (tmp_path / f"d{index}").mkdir()
    plane = ControlPlane(workspaces=FakeSource(), browsing=browsing(tmp_path))

    listing, _ = await listed(plane, str(tmp_path))
    await plane.close()

    assert isinstance(listing, DirectoryListing)
    assert (len(listing.entries), listing.truncated, listing.total) == (2000, True, 2001)


async def test_no_path_lists_the_home_directory(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    plane = ControlPlane(workspaces=FakeSource(), browsing=browsing(tmp_path))

    listing, _ = await listed(plane, None)
    await plane.close()

    assert isinstance(listing, DirectoryListing)
    assert (listing.path, [entry.name for entry in listing.entries]) == (str(tmp_path), ["a"])


async def test_only_the_connection_asking_gets_the_listing(tmp_path: Path) -> None:
    host = workspace("w")
    plane = ControlPlane([host], workspaces=FakeSource(), browsing=browsing(tmp_path))
    asking, other = await joined(plane, host), await joined(plane, host)
    taken(asking), taken(other)

    await say(asking, "list", ListDirectory(path=str(tmp_path)))
    answered, seen = taken(asking), taken(other)
    await plane.close()

    assert [type(event) for event in payloads(answered)] == [DirectoryListing, CommandAccepted]
    assert payloads(seen) == [CommandAccepted(command_id="list")]
    assert (answered[0].workspace, answered[0].session) == (None, None)


async def test_a_missing_directory_is_refused_as_unknown(tmp_path: Path) -> None:
    plane = ControlPlane(workspaces=FakeSource(), browsing=browsing(tmp_path))

    outcome = await listed(plane, str(tmp_path / "gone"))
    await plane.close()

    message = f"Can't list {tmp_path / 'gone'}: there's nothing there."
    assert outcome == [refused("list", UNKNOWN, message)]


async def test_an_unreadable_directory_is_refused_as_invalid(tmp_path: Path) -> None:
    class Unreadable(DirectoryLister):
        async def directories(self, path: str) -> list[FoundDirectory]:
            raise PermissionError(13, "Permission denied", path)

    plane = ControlPlane(workspaces=FakeSource(), browsing=Browsing(Unreadable(), str(tmp_path)))

    outcome = await listed(plane, str(tmp_path))
    await plane.close()

    assert outcome == [refused("list", INVALID, f"Permission denied: {tmp_path}")]


async def test_a_file_is_refused_as_invalid(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("")
    plane = ControlPlane(workspaces=FakeSource(), browsing=browsing(tmp_path))

    outcome = await listed(plane, str(tmp_path / "notes.txt"))
    await plane.close()

    message = f"Can't list {tmp_path / 'notes.txt'}: it isn't a directory."
    assert outcome == [refused("list", INVALID, message)]


async def test_a_relative_path_is_refused_as_invalid(tmp_path: Path) -> None:
    plane = ControlPlane(workspaces=FakeSource(), browsing=browsing(tmp_path))

    outcome = await listed(plane, "src")
    await plane.close()

    message = "Can't list src: give the absolute path to a directory."
    assert outcome == [refused("list", INVALID, message)]


async def test_a_local_orbit_refuses_listing_directories() -> None:
    plane = ControlPlane([workspace("a")])

    outcome = await listed(plane, "/src")
    await plane.close()

    assert outcome == [refused("list", INVALID, LOCAL_BROWSING)]


def fenced(tmp_path: Path) -> tuple[Path, Path]:
    """A browsable root ``x`` and a directory ``y`` beside it, holding a repository, with a
    symlink to ``y`` and a directory ``a`` inside ``x``."""
    x, y = tmp_path / "x", tmp_path / "y"
    (x / "a").mkdir(parents=True)
    (y / "repo").mkdir(parents=True)
    (x / "out").symlink_to(y)
    return x, y


async def test_outside_the_browsable_root_nothing_is_listed(tmp_path: Path) -> None:
    x, y = fenced(tmp_path)
    plane = ControlPlane(workspaces=FakeSource(), browsing=browsing(tmp_path, x))

    beside = await listed(plane, str(y))
    linked = await listed(plane, str(x / "out"))
    await plane.close()

    assert beside == [refused("list", INVALID, f"{y} is outside the browsable root {x}.")]
    message = f"{x / 'out'} is outside the browsable root {x}."
    assert linked == [refused("list", INVALID, message)]


async def test_a_directory_beside_the_root_sharing_its_name_is_outside(tmp_path: Path) -> None:
    x, _ = fenced(tmp_path)
    (tmp_path / "xa").mkdir()
    plane = ControlPlane(workspaces=FakeSource(), browsing=browsing(tmp_path, x))

    outcome = await listed(plane, str(tmp_path / "xa"))
    await plane.close()

    message = f"{tmp_path / 'xa'} is outside the browsable root {x}."
    assert outcome == [refused("list", INVALID, message)]


async def test_the_filesystems_root_has_no_parent(tmp_path: Path) -> None:
    plane = ControlPlane(workspaces=FakeSource(), browsing=browsing(tmp_path))

    listing, _ = await listed(plane, "/")
    await plane.close()

    assert isinstance(listing, DirectoryListing)
    assert (listing.path, listing.parent) == ("/", None)


async def test_inside_the_browsable_root_directories_are_listed_up_to_it(
    tmp_path: Path,
) -> None:
    x, _ = fenced(tmp_path)
    plane = ControlPlane(workspaces=FakeSource(), browsing=browsing(tmp_path, x))

    inside, _ = await listed(plane, str(x / "a"))
    top, _ = await listed(plane, None)
    await plane.close()

    assert isinstance(inside, DirectoryListing) and inside.parent == str(x)
    assert isinstance(top, DirectoryListing)
    assert (top.path, top.parent) == (str(x), None)
    assert [entry.name for entry in top.entries] == ["a", "out"]


async def test_outside_the_browsable_root_nothing_is_added(tmp_path: Path) -> None:
    x, y = fenced(tmp_path)
    source = FakeSource()
    plane = ControlPlane(workspaces=source, browsing=browsing(tmp_path, x))
    connection = await welcomed(plane)

    await say(connection, "add", AddWorkspace(path=str(y / "repo")))
    await say(connection, "linked", AddWorkspace(path=str(x / "out" / "repo")))
    outcomes = payloads(taken(connection))
    await say(connection, "inside", AddWorkspace(path=str(x / "a")))
    inside = payloads(taken(connection))
    await plane.close()

    out, linked = f"{y / 'repo'}", f"{x / 'out' / 'repo'}"
    assert outcomes == [
        refused("add", INVALID, f"{out} is outside the browsable root {x}."),
        refused("linked", INVALID, f"{linked} is outside the browsable root {x}."),
    ]
    assert inside[-1] == CommandAccepted(command_id="inside")
    assert source.opened == [str(x / "a")]


async def test_a_clone_is_added_wherever_the_clone_directory_is(tmp_path: Path) -> None:
    x, _ = fenced(tmp_path)
    source = FakeSource()
    plane = ControlPlane(workspaces=source, browsing=browsing(tmp_path, x))
    connection = await welcomed(plane)

    await say(connection, "clone", AddWorkspace(url="https://github.com/o/proj.git"))
    while not source.opened:
        await asyncio.sleep(0.01)
    await plane.close()

    assert source.managed == ["/clones/proj.git"]
