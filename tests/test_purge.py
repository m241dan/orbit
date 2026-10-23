"""Purging a workspace, the one use case ``orbit db forget`` and a server removing a workspace
with ``purge`` share: refused, deleting nothing, if it isn't registered, another orbit holds it
or its clone's work would be lost; else stopped, its files deleted, then its records."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from orbit.agent.application.purge import erase, purge
from orbit.agent.domain.purging import Purged, PurgeRefused
from orbit.agent.domain.workspaces import RegisteredWorkspace
from orbit.protocol import BUSY, INVALID

PROJECT = RegisteredWorkspace("w", "/src/proj", served=False, managed=False)
CLONE = RegisteredWorkspace("w", "/clones/proj", served=False, managed=True)


class FakePurging:
    """Purging the ``workspace`` registered, if any, at its path or id, held by another orbit
    as ``holder`` says, if one does, losing what ``lost`` says, failing to remove its clone as
    ``clone_fails`` says, if it does; each step is noted in ``steps``, in order."""

    def __init__(self, workspace: RegisteredWorkspace | None = PROJECT) -> None:
        self.workspace = workspace
        self.holder: str | None = None
        self.lost: list[str] = []
        self.clone_fails: str | None = None
        self.steps: list[str] = []

    async def find(self, key: str) -> RegisteredWorkspace | None:
        self.steps.append(f"find {key}")
        found = self.workspace
        return found if found is not None and key in (found.id, found.path) else None

    @asynccontextmanager
    async def held(self, workspace: RegisteredWorkspace) -> AsyncIterator[str | None]:
        self.steps.append("hold")
        try:
            yield self.holder
        finally:
            self.steps.append("let go")

    async def losses(self, workspace: RegisteredWorkspace) -> list[str]:
        self.steps.append("losses")
        return self.lost

    async def stop(self, workspace: RegisteredWorkspace) -> None:
        self.steps.append("stop")

    async def delete(self, workspace: RegisteredWorkspace) -> Purged:
        return await erase(self, workspace)

    async def remove_fallback_files(self, workspace: RegisteredWorkspace) -> int:
        self.steps.append("fallback files")
        return 2

    async def remove_session_locks(self, workspace_id: str) -> None:
        self.steps.append("session locks")

    async def remove_clone(self, path: str) -> None:
        self.steps.append(f"clone {path}")
        if self.clone_fails is not None:
            raise OSError(self.clone_fails)

    async def remove_records(self, workspace_id: str) -> int:
        self.steps.append("records")
        return 3


async def test_a_workspace_is_stopped_then_its_files_then_its_records_deleted_while_held() -> None:
    purging = FakePurging()

    purged = await purge(purging, "/src/proj")

    assert purged == Purged(PROJECT, sessions=3, fallback_files=2, clone=None)
    assert purging.steps == [
        "find /src/proj",
        "hold",
        "losses",
        "stop",
        "fallback files",
        "session locks",
        "records",
        "let go",
    ]


async def test_a_managed_clone_has_its_directory_deleted_before_its_records() -> None:
    purging = FakePurging(CLONE)

    purged = await purge(purging, "w")

    assert purged.clone == "/clones/proj"
    assert purging.steps[4:7] == ["fallback files", "session locks", "clone /clones/proj"]
    assert purging.steps[7:] == ["records", "let go"]


async def test_the_loss_check_runs_once_per_purge() -> None:
    purging = FakePurging(CLONE)

    await purge(purging, "w")

    assert purging.steps.count("losses") == 1


async def test_a_workspace_not_registered_is_refused_as_invalid_touching_nothing() -> None:
    purging = FakePurging(None)

    with pytest.raises(PurgeRefused) as refusal:
        await purge(purging, "/src/nope")

    assert (refusal.value.code, str(refusal.value)) == (
        INVALID,
        "No workspace is registered at /src/nope.",
    )
    assert purging.steps == ["find /src/nope"]


async def test_a_workspace_another_orbit_holds_is_refused_as_busy_before_its_losses() -> None:
    purging = FakePurging()
    purging.holder = "Can't forget /src/proj: an orbit chat, pid 42 holds its lock."

    with pytest.raises(PurgeRefused) as refusal:
        await purge(purging, "w")

    assert (refusal.value.code, str(refusal.value)) == (BUSY, purging.holder)
    assert purging.steps == ["find w", "hold", "let go"]


async def test_a_clone_whose_work_would_be_lost_is_refused_as_invalid_before_it_stops() -> None:
    purging = FakePurging(CLONE)
    purging.lost = ["1 stash"]

    with pytest.raises(PurgeRefused) as refusal:
        await purge(purging, "w")

    assert (refusal.value.code, str(refusal.value)) == (
        INVALID,
        "Can't purge /clones/proj: deleting its clone would lose 1 stash.",
    )
    assert purging.steps == ["find w", "hold", "losses", "let go"]


async def test_a_clone_that_cant_be_deleted_keeps_its_records_so_purging_again_works() -> None:
    purging = FakePurging(CLONE)
    purging.clone_fails = "Permission denied"

    with pytest.raises(OSError, match="Permission denied"):
        await purge(purging, "w")
    assert "records" not in purging.steps

    purging.clone_fails = None
    purged = await purge(purging, "w")
    assert purged.clone == "/clones/proj" and purging.steps[-2:] == ["records", "let go"]
