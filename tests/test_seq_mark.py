"""The store's mark of the ``seq`` handed out, as a control plane given it reserves them there:
past the mark when it serves no workspace, once per block however many it serves, and
whenever a block is crossed, even as a workspace is being added."""

import asyncio
from collections.abc import Callable
from pathlib import Path

from orbit.agent import CloneFailed, ControlPlane, PathKind, SqliteStore, WorkspaceHost
from orbit.agent.application.outbox import SEQ_BLOCK
from orbit.agent.application.seq_mark import MarkReservations
from orbit.protocol import AddWorkspace, WorkspaceCloning
from tests.agent_fakes import HELLO, FakeStore
from tests.plane_fakes import say, taken


async def greeted(plane: ControlPlane) -> list[int]:
    """The ``seq`` of what a new connection to ``plane`` gets for its Hello."""
    connection = plane.connect()
    await say(connection, "hello", HELLO)
    return [envelope.seq for envelope in taken(connection)]


async def marked(state: Path, seq: int) -> None:
    """Have the store in ``state`` reserve every ``seq`` up to ``seq``."""
    store = await SqliteStore.open(state)
    await store.reserve(seq)
    await store.close()


async def mark_of(state: Path) -> int:
    """The highest ``seq`` the store in ``state`` counts, opened again."""
    store = await SqliteStore.open(state)
    await store.close()
    return store.max_seq()


async def served_empty(state: Path, connections: int) -> list[int]:
    """The ``seq`` an orbit serving no workspace over the store in ``state`` stamped for the
    Hello of each of ``connections``, after which it closed."""
    store = await SqliteStore.open(state)
    plane = ControlPlane(mark=store)
    seqs = [seq for _ in range(connections) for seq in await greeted(plane)]
    await plane.close()
    await store.close()
    return seqs


async def test_a_control_plane_serving_nothing_stamps_past_the_stores_mark(tmp_path: Path) -> None:
    await marked(tmp_path, 500)

    seqs = await served_empty(tmp_path, 1)

    assert min(seqs) > 500


async def test_an_empty_server_restarted_stamps_past_every_seq_it_handed_out(
    tmp_path: Path,
) -> None:
    before = await served_empty(tmp_path, SEQ_BLOCK)
    assert max(before) > SEQ_BLOCK

    after = await served_empty(tmp_path, 1)

    assert min(after) > max(before)


class GatedSource:
    """Workspaces for a control plane to serve, each path a directory standing for itself, whose
    hosts wait for ``gate`` before opening their latest session, set ``opening`` once they do."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.opening = asyncio.Event()

    async def kind(self, path: str) -> PathKind:
        return PathKind.DIRECTORY

    async def top_level(self, path: str) -> str:
        return path

    async def clone(self, url: str, progress: Callable[[WorkspaceCloning], None]) -> str:
        raise CloneFailed("Nothing is cloned here.")

    async def open(self, root: str, *, managed: bool = False) -> WorkspaceHost:
        source = self

        class Gated(WorkspaceHost):
            async def open_latest(self) -> None:
                source.opening.set()
                await source.gate.wait()
                await super().open_latest()

        return Gated(workspace=root, workspace_id="c", new_id=lambda: "c-1")

    async def set_served(self, workspace_id: str, served: bool) -> None:
        pass

    async def latest_added(self) -> str | None:
        return None

    async def close(self, workspace_id: str) -> None:
        pass

    async def losses(self, workspace_id: str) -> list[str]:
        return []

    async def purge(self, workspace_id: str) -> None:
        pass


async def adding(plane: ControlPlane, source: GatedSource) -> int:
    """The highest ``seq`` stamped for other connections to ``plane`` while one adds a workspace
    from ``source``, once it's added."""
    connection = plane.connect()
    await say(connection, "hello", HELLO)
    added = asyncio.create_task(say(connection, "add", AddWorkspace(path="/src/c")))
    await source.opening.wait()
    meanwhile = [seq for _ in range(SEQ_BLOCK) for seq in await greeted(plane)]
    source.gate.set()
    await added
    return max(meanwhile)


async def test_a_block_crossed_while_a_workspace_is_added_is_recorded(tmp_path: Path) -> None:
    store = await SqliteStore.open(tmp_path)
    source = GatedSource()
    plane = ControlPlane(workspaces=source, mark=store)

    highest = await adding(plane, source)
    await plane.close()
    await store.close()

    assert highest > SEQ_BLOCK
    assert await mark_of(tmp_path) >= highest


async def test_one_block_is_reserved_once_however_many_workspaces_are_served() -> None:
    mark, first, second = FakeStore(), FakeStore(), FakeStore()
    hosts = [
        WorkspaceHost(workspace_id=name, store=store)
        for name, store in [("a", first), ("b", second)]
    ]
    plane = ControlPlane(hosts, mark=mark)

    await greeted(plane)
    await plane.close()

    assert mark.reservations == [SEQ_BLOCK]
    assert first.reservations == second.reservations == []


async def test_the_seqs_stamped_start_past_the_mark_and_the_hosts_logs() -> None:
    mark, log = FakeStore(), FakeStore()
    mark.seq, log.seq = 300, 200
    plane = ControlPlane([WorkspaceHost(workspace_id="a", store=log)], mark=mark)

    seqs = await greeted(plane)
    await plane.close()

    assert min(seqs) == 301


async def test_the_highest_seq_asked_for_is_reserved_once() -> None:
    mark = FakeStore()
    reservations = MarkReservations(mark)

    reservations.reserve(100)
    reservations.reserve(200)
    await reservations.close()

    assert mark.reservations == [200]


async def test_a_seq_asked_for_while_reserving_is_reserved_next() -> None:
    mark = FakeStore()
    reservations = MarkReservations(mark)

    reservations.reserve(100)
    await asyncio.sleep(0)  # Reserving, which takes a while.
    reservations.reserve(200)
    await reservations.close()

    assert mark.reservations == [100, 200]


async def test_a_seq_asked_for_once_reserving_is_done_is_reserved() -> None:
    mark = FakeStore()
    reservations = MarkReservations(mark)

    reservations.reserve(100)
    await asyncio.sleep(0.01)
    reservations.reserve(200)
    await asyncio.sleep(0.01)

    assert mark.reservations == [100, 200]
    await reservations.close()


async def test_a_failed_reservation_is_made_again_as_it_closes() -> None:
    mark = FakeStore()
    mark.reserve_failure = "disk full"
    reservations = MarkReservations(mark)

    reservations.reserve(100)
    await asyncio.sleep(0.01)
    mark.reserve_failure = None
    await reservations.close()

    assert mark.reservations == [100]


async def test_nothing_is_reserved_once_closed() -> None:
    mark = FakeStore()
    reservations = MarkReservations(mark)
    await reservations.close()

    reservations.reserve(100)
    await asyncio.sleep(0.01)

    assert mark.reservations == []
