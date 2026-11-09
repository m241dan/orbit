"""A change to one task, such as a turn's usage, published, logged and saved as that task alone:
what it logs and saves, and that a frontend catching up from the log ends with the board a live
one shows."""

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from functools import reduce

from orbit.agent import HostParts, WorkspaceHost
from orbit.protocol import (
    PROTOCOL_VERSION,
    CommandEnvelope,
    Event,
    EventEnvelope,
    Hello,
    Snapshot,
    TaskChanged,
    TasksChanged,
    TaskSpec,
    TaskState,
    TurnFinished,
    Usage,
)
from tests.agent_fakes import (
    ATTACH_ID,
    HELLO_ID,
    FakeStore,
    Ticks,
    attach_to,
    board,
    claims_done,
    delivered,
    frontend,
    next_board,
    run,
    session_for,
    snapshot_of,
)
from tests.session_builders import OPEN

USAGE = Usage(input_tokens=10, output_tokens=5)
"""What each agent's one turn costs."""


@dataclass(frozen=True)
class Ran:
    """What a session showed as it ran its tasks: the board a frontend following its events
    ``live`` ended with, and the one a ``snapshot`` of it carried then."""

    live: tuple[TaskState, ...]
    snapshot: tuple[TaskState, ...]


async def ran(store: FakeStore, *prompts: str) -> Ran:
    """What a session over ``store`` showed as it ran a task for each of ``prompts``, each
    agent's one turn costing :data:`USAGE`, closed once they ran, its events all logged."""
    session, _, launcher = session_for(
        *(claims_done for _ in prompts), store=store, new_id=lambda: OPEN
    )
    launcher.usages = [(USAGE,) for _ in prompts]
    live = await asyncio.wait_for(run(session, *(TaskSpec(prompt=p) for p in prompts)), 5)
    shown = Ran(board(live), (await snapshot_of(session)).tasks)
    await session.close()
    return shown


def logged_after_usage(store: FakeStore) -> list[Event]:
    """The payloads logged after the first turn that reported usage."""
    payloads = [envelope.payload for envelope in store.logged]
    turn = next(
        i for i, p in enumerate(payloads) if isinstance(p, TurnFinished) and p.usage is not None
    )
    return payloads[turn + 1 :]


def board_events(payloads: Iterable[Event]) -> list[TasksChanged | TaskChanged]:
    """The ``TasksChanged`` and ``TaskChanged`` among ``payloads``, in order."""
    return [p for p in payloads if isinstance(p, TasksChanged | TaskChanged)]


def running_with_usage(event: TasksChanged | TaskChanged) -> bool:
    """Whether ``event`` publishes a running task that spent :data:`USAGE`."""
    tasks = event.tasks if isinstance(event, TasksChanged) else (event.task,)
    return any(t.status == "running" and t.usage == USAGE for t in tasks)


async def test_a_turns_usage_is_logged_as_its_task_alone() -> None:
    store = FakeStore()
    await ran(store, "one", "two")

    published = board_events(logged_after_usage(store))
    assert isinstance(published[0], TaskChanged)
    assert running_with_usage(published[0])
    assert not any(running_with_usage(e) for e in published if isinstance(e, TasksChanged))


async def test_a_turns_usage_is_saved_with_its_tasks_end_not_at_once() -> None:
    store = FakeStore()
    await ran(store, "one")

    saved = [(r.tasks[0].status, r.tasks[0].usage) for r in store.saved]
    assert ("running", Usage()) in saved
    assert ("running", USAGE) not in saved
    assert saved[-1] == ("completed", USAGE)


async def caught_up(store: FakeStore, since: int) -> list[EventEnvelope]:
    """The envelopes a session reopened over ``store`` resends a frontend that saw its open
    session's events up to ``since``, as it catches up on what it missed."""
    session = WorkspaceHost(HostParts(store=store, clock=Ticks(), new_id=lambda: OPEN))
    hello = Hello(client="orbit-tests", protocol_version=PROTOCOL_VERSION, since=since)
    await frontend(session).send(CommandEnvelope(command_id=HELLO_ID, payload=hello))
    attaching = attach_to(session, OPEN)
    await frontend(session).send(CommandEnvelope(command_id=ATTACH_ID, payload=attaching))
    await session.close()
    return delivered(session)


def first_board(store: FakeStore) -> tuple[int, tuple[TaskState, ...]]:
    """The ``seq`` of the first ``TasksChanged`` ``store`` logged, and the board it published."""
    first = next(e for e in store.logged if isinstance(e.payload, TasksChanged))
    assert isinstance(first.payload, TasksChanged)
    return first.seq, first.payload.tasks


def replayed(sent: Iterable[EventEnvelope], since: int, last: int) -> list[Event]:
    """The payloads of ``sent`` after ``since`` and up to ``last``, those a frontend that saw the
    events up to ``since`` catches up on, rather than those stamped since."""
    return [e.payload for e in sent if since < e.seq <= last]


async def test_a_frontend_catching_up_from_the_log_ends_with_the_live_board() -> None:
    store = FakeStore()
    held = (await ran(store, "one", "two")).live
    since, seen = first_board(store)
    last = store.logged[-1].seq

    sent = await caught_up(store, since)

    assert Snapshot not in {type(envelope.payload) for envelope in sent}
    missed = board_events(replayed(sent, since, last))
    assert TaskChanged in {type(event) for event in missed}
    assert reduce(next_board, missed, seen) == held
    assert [t.usage for t in held] == [USAGE, USAGE]


async def test_a_snapshot_carries_the_board_a_live_frontend_ends_with() -> None:
    shown = await ran(FakeStore(), "one", "two")

    assert shown.snapshot == shown.live
    assert [t.usage for t in shown.snapshot] == [USAGE, USAGE]
