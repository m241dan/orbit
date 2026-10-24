"""The served set on its own: a change it refuses once the control plane is closing leaves the
change lock free and starts nothing."""

import asyncio

import pytest

from orbit.agent import ControlPlane
from orbit.agent.application.clones import Clones
from orbit.agent.application.coder_slots import CoderSlots
from orbit.agent.application.connection import SessionConnection
from orbit.agent.application.served_set import ServedParts, ServedSet
from orbit.agent.application.workspace_host import WorkspaceHost
from orbit.agent.domain.refusals import STOPPING, Refused
from orbit.protocol import INVALID, AddWorkspace, Event, RemoveWorkspace
from tests.plane_fakes import FakeSource


def _untold(event: Event) -> None:
    """Tell no one of ``event``."""


def _unserved(host: WorkspaceHost) -> None:
    """Wire ``host`` into nothing."""


async def _unbegun(host: WorkspaceHost) -> None:
    """Begin nothing for ``host``."""


def _undetached(connection: SessionConnection) -> None:
    """Detach ``connection`` from nothing."""


async def _unchanged(connection: SessionConnection | None) -> None:
    """Send no catalog."""


def _closing_parts(source: FakeSource, changing: asyncio.Lock) -> ServedParts:
    """The parts of a served set over ``source`` whose control plane is closing."""
    return ServedParts(
        hosts={},
        workspaces=source,
        changing=changing,
        closing=lambda: True,
        coders=CoderSlots(),
        clones=Clones(_untold),
        browsing=None,
        serve=_unserved,
        begin=_unbegun,
        detach=_undetached,
        changed=_unchanged,
        totals_changed=lambda: None,
    )


@pytest.mark.parametrize(
    "command",
    [AddWorkspace(path="/src/c"), RemoveWorkspace(workspace="c")],
    ids=["add", "remove"],
)
async def test_a_change_refused_once_closing_leaves_the_lock_free(
    command: AddWorkspace | RemoveWorkspace,
) -> None:
    source, changing = FakeSource(), asyncio.Lock()
    served = ServedSet(_closing_parts(source, changing))

    with pytest.raises(Refused) as refused:
        await served.change(command, ControlPlane().connect())

    assert (refused.value.code, str(refused.value)) == (INVALID, STOPPING)
    assert not changing.locked() and source.opened == []


async def test_a_clone_refused_once_closing_starts_no_clone() -> None:
    source = FakeSource()
    served = ServedSet(_closing_parts(source, asyncio.Lock()))

    with pytest.raises(Refused):
        await served.change(AddWorkspace(url="https://example.com/r.git"), ControlPlane().connect())
    await asyncio.sleep(0)

    assert source.cloning == []
