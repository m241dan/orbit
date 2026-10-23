"""Catching a reconnecting frontend up on the events it missed: when it can, and what it gets."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from orbit.agent import ControlPlane, WorkspaceHost
from orbit.agent.domain.replay import LogBounds, can_replay
from orbit.agent.domain.sessions import SessionRecord
from orbit.protocol import (
    PROTOCOL_VERSION,
    Catalog,
    CommandAccepted,
    CommandEnvelope,
    EventEnvelope,
    Hello,
    Notice,
    SessionStateChanged,
    Snapshot,
    Welcome,
)
from tests.agent_fakes import (
    ATTACH_ID,
    HELLO_ID,
    FakeStore,
    GatedLog,
    Ticks,
    attach_to,
    delivered,
    frontend,
    notice,
    publish,
    served,
)
from tests.connection_helpers import taken
from tests.session_builders import OPEN, store_logging
from tests.store_views import OwnView, database, open_view

WHEN = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)

# The rule


@pytest.mark.parametrize(
    ("since", "bounds"),
    [
        (15, LogBounds(first_seq=10, others_seq=0, own_seq=15)),
        (20, LogBounds(first_seq=10, others_seq=0, own_seq=20)),
        (15, LogBounds(first_seq=10, others_seq=5, own_seq=12)),
        (15, LogBounds(first_seq=4, others_seq=13, own_seq=14)),
    ],
)
def test_a_frontend_catches_up_on_the_open_sessions_events_alone(
    since: int, bounds: LogBounds
) -> None:
    assert can_replay(since, 20, bounds)


@pytest.mark.parametrize(
    ("since", "bounds"),
    [
        (15, LogBounds(first_seq=10, others_seq=18, own_seq=15)),  # Another's came after it.
        (8, LogBounds(first_seq=10, others_seq=0, own_seq=7)),  # Older than what's kept.
        (21, LogBounds(first_seq=10, others_seq=0, own_seq=20)),  # Ahead of the last stamped.
        (15, LogBounds(first_seq=None, others_seq=15, own_seq=None)),  # It saw none of its own.
        (15, LogBounds(first_seq=16, others_seq=0, own_seq=None)),  # Its first came after it.
        (15, LogBounds(first_seq=1, others_seq=15, own_seq=5)),  # Another's came after its own.
    ],
)
def test_a_frontend_cant_catch_up_otherwise(since: int, bounds: LogBounds) -> None:
    assert not can_replay(since, 20, bounds)


# What the session sends


def notices(first: int, end: int) -> list[EventEnvelope]:
    return [notice(seq, OPEN) for seq in range(first, end)]


async def rejoin(session: WorkspaceHost, since: int | None, attach: str = OPEN) -> None:
    """Greet ``session`` with ``since``, then attach to its session ``attach``."""
    hello = Hello(client="orbit-tests", protocol_version=PROTOCOL_VERSION, since=since)
    await frontend(session).send(CommandEnvelope(command_id=HELLO_ID, payload=hello))
    attaching = attach_to(session, attach)
    await frontend(session).send(CommandEnvelope(command_id=ATTACH_ID, payload=attaching))


async def reconnected(
    store: FakeStore | None, since: int | None, attach: str = OPEN
) -> list[EventEnvelope]:
    """What a session over ``store`` sends a frontend greeting it with ``since``, then
    attaching to its session ``attach``."""
    session = WorkspaceHost(store=store, clock=Ticks(), new_id=lambda: OPEN, workspace="/work")
    await rejoin(session, since, attach)
    await session.close()
    return delivered(session)


def kinds(sent: list[EventEnvelope]) -> list[type]:
    return [type(envelope.payload) for envelope in sent]


async def test_a_reconnecting_frontend_gets_what_it_missed_in_place_of_a_snapshot() -> None:
    store = store_logging(*(notice(seq, OPEN) for seq in range(10, 21)))
    sent = await reconnected(store, 15)

    assert kinds(sent[:2]) == [Welcome, Catalog]
    assert sent[2:7] == notices(16, 21)
    assert sent[7].payload == Notice(message="Workspace: /work")
    assert sent[8].payload == CommandAccepted(command_id=ATTACH_ID)
    assert Snapshot not in kinds(sent)
    assert isinstance(sent[-1].payload, SessionStateChanged)  # Counting the frontend, unlogged.
    assert store.appends == []  # Neither the events resent nor the greeting are logged again.


async def test_events_still_to_be_written_are_caught_up_on_once() -> None:
    store = GatedLog()
    session = WorkspaceHost(store=store, clock=Ticks(), new_id=lambda: OPEN)
    await publish(session, Notice(message="a"))
    await store.waiting.wait()  # 1 is being written.
    await publish(session, Notice(message="b"))
    await publish(session, Notice(message="c"))
    delivered(session)  # Sent before the frontend went away.
    await rejoin(session, 1)
    store.gate.set()
    await session.close()

    replayed = [e for e in delivered(session) if isinstance(e.payload, Notice)]
    assert [(e.seq, e.payload) for e in replayed] == [
        (2, Notice(message="b")),
        (3, Notice(message="c")),
    ]


@pytest.mark.parametrize(
    "logged",
    [
        [notice(seq, OPEN) for seq in range(10, 18)] + [notice(18, "other")],
        [notice(seq, OPEN) for seq in range(17, 21)],
        [notice(seq, "other") for seq in range(1, 16)],
        [notice(seq, OPEN) for seq in range(1, 6)] + [notice(seq, "other") for seq in range(6, 16)],
        [notice(seq, OPEN) for seq in range(16, 21)],
    ],
    ids=[
        "another session came after it",
        "older than what's kept",
        "the open session is new",
        "the open session's came before another's",
        "the open session's came after it",
    ],
)
async def test_a_frontend_that_cant_catch_up_gets_a_snapshot(logged: list[EventEnvelope]) -> None:
    sent = await reconnected(store_logging(*logged), 15)

    assert kinds(sent[:3]) == [Welcome, Catalog, Snapshot]
    assert not [envelope for envelope in sent if envelope.payload == Notice(message="n16")]


async def test_a_frontend_attaching_to_a_saved_session_gets_its_view_not_a_replay() -> None:
    store = store_logging(*(notice(seq, OPEN) for seq in range(10, 21)), notice(9, "earlier"))
    store.saved.append(SessionRecord(id="earlier", name="earlier", created=WHEN, last_active=WHEN))
    sent = await reconnected(store, 15, attach="earlier")

    [snapshot] = [e.payload for e in sent if isinstance(e.payload, Snapshot)]
    assert (snapshot.session.id, snapshot.active) == ("earlier", False)
    assert not [envelope for envelope in sent if envelope.payload == Notice(message="n16")]


async def test_a_hello_gets_nothing_it_missed_until_it_attaches() -> None:
    store = store_logging(*(notice(seq, OPEN) for seq in range(10, 21)))
    session = WorkspaceHost(store=store, clock=Ticks(), new_id=lambda: OPEN)
    hello = Hello(client="orbit-tests", protocol_version=PROTOCOL_VERSION, since=15)
    await frontend(session).send(CommandEnvelope(command_id=HELLO_ID, payload=hello))
    await session.close()

    assert kinds(delivered(session)) == [Welcome, Catalog]


async def test_a_frontend_ahead_of_the_log_gets_a_snapshot() -> None:
    sent = await reconnected(store_logging(*(notice(seq, OPEN) for seq in range(10, 21))), 21)
    assert kinds(sent[:3]) == [Welcome, Catalog, Snapshot]


async def test_a_frontend_with_no_store_to_catch_up_from_gets_a_snapshot() -> None:
    sent = await reconnected(None, 0)
    assert kinds(sent[:3]) == [Welcome, Catalog, Snapshot]


async def test_a_frontend_that_gave_no_seq_gets_a_snapshot() -> None:
    sent = await reconnected(store_logging(*(notice(seq, OPEN) for seq in range(10, 21))), None)
    assert kinds(sent[:3]) == [Welcome, Catalog, Snapshot]


class Unreadable(FakeStore):
    """A log whose bounds can be read, but not its events."""

    async def after(self, session_id: str, after_seq: int) -> list[EventEnvelope] | None:
        return None


async def test_a_frontend_whose_missed_events_cant_be_read_gets_a_snapshot() -> None:
    store = Unreadable()
    store.logged = [notice(1, OPEN)]
    sent = await reconnected(store, 1)
    assert kinds(sent[:3]) == [Welcome, Catalog, Snapshot]


# After a restart


async def greeted(
    plane: ControlPlane, host: WorkspaceHost, since: int | None
) -> list[EventEnvelope]:
    """What a new connection to ``plane`` gets for its Hello giving ``since`` and, when it gives
    one, its Attach to ``host``'s open session."""
    connection = plane.connect()
    hello = Hello(client="orbit-tests", protocol_version=PROTOCOL_VERSION, since=since)
    await connection.send(CommandEnvelope(command_id=HELLO_ID, payload=hello))
    if since is not None:
        attach = CommandEnvelope(command_id=ATTACH_ID, payload=attach_to(host))
        await connection.send(attach)
    return taken(connection)


def highest_logged(state: Path) -> int:
    """The highest ``seq`` the store in ``state`` logged, kept or forgotten since."""
    with database(state) as connection:
        (seq,) = connection.execute(
            """SELECT max(coalesce((SELECT max(seq) FROM events), 0),
                (SELECT max(forgotten_seq) FROM workspaces))"""
        ).fetchone()
        return int(seq)


async def ran_unlogged(state: Path, workspace: Path) -> int:
    """The highest ``seq`` an orbit over a store in ``state`` handed out, having logged an event
    and then stamped the control plane's own, never logged, for frontends saying Hello, after
    which it closed."""
    store = await open_view(state, workspace)
    first = WorkspaceHost(store=store, clock=Ticks(), new_id=lambda: "session-1")
    await publish(first, Notice(message="logged"))
    plane = served(first)
    seqs = [envelope.seq for _ in range(3) for envelope in await greeted(plane, first, None)]
    await plane.close()
    await store.close()
    assert highest_logged(state) < max(seqs)  # The last seqs handed out were never logged.
    return max(seqs)


async def restarted(state: Path, workspace: Path) -> tuple[OwnView, WorkspaceHost, ControlPlane]:
    """An orbit over the store in ``state`` opened again, its open session new."""
    store = await open_view(state, workspace)
    host = WorkspaceHost(store=store, clock=Ticks(), new_id=lambda: "session-2")
    return store, host, served(host)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path


async def test_a_restarted_orbit_stamps_past_every_seq_handed_out_though_never_logged(
    tmp_path: Path, workspace: Path
) -> None:
    handed_out = await ran_unlogged(tmp_path / "state", workspace)
    store, host, plane = await restarted(tmp_path / "state", workspace)
    sent = await greeted(plane, host, None)
    await plane.close()
    await store.close()

    assert min(envelope.seq for envelope in sent) > handed_out


async def test_a_frontend_that_saw_unlogged_events_before_a_restart_gets_a_snapshot(
    tmp_path: Path, workspace: Path
) -> None:
    since = await ran_unlogged(tmp_path / "state", workspace)
    store, host, plane = await restarted(tmp_path / "state", workspace)
    for seq in range(20):  # Past the seqs handed out before, if they were handed out again.
        await publish(host, Notice(message=f"n{seq}"))
    sent = await greeted(plane, host, since)
    await plane.close()
    await store.close()

    assert kinds(sent[:3]) == [Welcome, Catalog, Snapshot]
