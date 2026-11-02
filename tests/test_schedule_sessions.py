"""``schedule`` triggers in a session: opening fires once, after the ``session_started``
triggers, each that came due since it last fired, however often, a trigger first seen not at
all; then each fires at its time, queuing its role's task or running its set; each workspace
open runs its own schedules, and closing one stops them."""

from datetime import UTC, datetime, timedelta
from typing import Any

from orbit.agent import Verification, VerificationStep, WorkspaceHost
from orbit.agent.domain.triggers import Trigger
from orbit.agent.domain.verification import CheckSet
from orbit.protocol import CODER, TasksChanged
from tests.agent_fakes import (
    TRIGGERS,
    FakeStore,
    FakeTree,
    Steps,
    agent_exited,
    board_of,
    events_until,
    greeted,
    schedules_of,
    session_for,
)
from tests.schedule_clock import FakeScheduleClock

TUESDAY_MORNING = datetime(2026, 3, 3, 8, tzinfo=UTC)
WEDNESDAY_NIGHT = datetime(2026, 3, 4, 3, tzinfo=UTC)
THURSDAY_NIGHT = datetime(2026, 3, 5, 3, tzinfo=UTC)
A_WEEK_AGO = TUESDAY_MORNING - timedelta(days=7)

HELLO = Trigger(on="session_started", role=CODER, prompt="Say hello", when={"verification": "off"})
"""A project's trigger queuing a coder's hello as a workspace opens without verification."""

TIDY = Trigger(on="schedule", cron="0 3 * * *", role=CODER, prompt="Tidy up after {trigger}")
"""A project's trigger queuing a coder's tidying at 3 every night."""

MUTATION = VerificationStep("Mutation tests", ("mutmut", "run"), "mutation")
NIGHTLY_MUTATION = Trigger(on="schedule", cron="0 3 * * *", checks="mutation")
"""A project's trigger running the set mutation at 3 every night."""


def opened(
    clock: FakeScheduleClock, store: FakeStore, **triggers: Trigger
) -> tuple[WorkspaceHost, FakeStore]:
    """A session opening a workspace in a tree without verification, keeping its sessions and
    schedules in ``store``, its schedules on ``clock``, with the shipped triggers and
    ``triggers``."""
    session = scheduled(clock, store, triggers={**TRIGGERS, **triggers})
    return session, store


def scheduled(clock: FakeScheduleClock, store: FakeStore, **options: Any) -> WorkspaceHost:
    """A session opening a workspace in a tree, keeping its sessions and schedules in
    ``store``, its schedules on ``clock``; ``options`` go to :func:`session_for`."""
    session, _, _ = session_for(
        tree=FakeTree(),
        check_at_startup=True,
        store=store,
        schedule_clock=clock.schedule_clock,
        **options,
    )
    return session


def kept(fired: dict[str, datetime]) -> FakeStore:
    """A store keeping ``fired`` as the times the triggers last fired."""
    store = FakeStore()
    store.fired = dict(fired)
    return store


def shown(session: WorkspaceHost) -> list[tuple[str, str]]:
    """Each task on ``session``'s board: its origin and prompt."""
    return [(state.origin, state.task.prompt) for state in board_of(session).tasks]


def tasks_counted(count: int) -> Any:
    """Stops once the board holds ``count`` tasks."""
    return lambda event: isinstance(event, TasksChanged) and len(event.tasks) == count


async def test_opening_fires_a_schedule_missed_while_closed_once_after_session_started() -> None:
    clock = FakeScheduleClock(TUESDAY_MORNING)
    session, store = opened(clock, kept({"tidy": A_WEEK_AGO}), hello=HELLO, tidy=TIDY)
    await events_until(session, tasks_counted(2))
    await clock.parked()
    await session.close()

    assert shown(session) == [("hello", "Say hello"), ("tidy", "Tidy up after tidy")]
    assert store.fired == {"tidy": TUESDAY_MORNING}


async def test_opening_doesnt_fire_a_schedule_trigger_first_seen() -> None:
    clock = FakeScheduleClock(TUESDAY_MORNING)
    session, store = opened(clock, FakeStore(), hello=HELLO, tidy=TIDY)
    await events_until(session, tasks_counted(1))
    await clock.parked()
    await session.close()

    assert shown(session) == [("hello", "Say hello")]
    assert store.fired == {"tidy": TUESDAY_MORNING}


async def test_a_schedule_triggers_task_is_queued_at_its_time() -> None:
    clock = FakeScheduleClock(TUESDAY_MORNING)
    session, store = opened(clock, FakeStore(), tidy=TIDY)
    await greeted(session)
    await clock.parked()
    await clock.go_to(WEDNESDAY_NIGHT - timedelta(seconds=1))
    assert shown(session) == []

    await clock.go_to(WEDNESDAY_NIGHT)
    await session.close()

    assert shown(session) == [("tidy", "Tidy up after tidy")]
    assert store.fired == {"tidy": WEDNESDAY_NIGHT}


async def test_a_schedule_runs_its_set_at_its_time_and_again_at_the_next() -> None:
    steps = Steps(True, True)
    clock = FakeScheduleClock(TUESDAY_MORNING)
    off = TRIGGERS["startup-check"].model_copy(update={"enabled": False})
    session = scheduled(
        clock,
        FakeStore(),
        verification=Verification((), steps, sets=(CheckSet("mutation", (MUTATION,)),)),
        triggers={**TRIGGERS, "startup-check": off, "nightly": NIGHTLY_MUTATION},
    )
    await greeted(session)
    await clock.parked()

    await clock.go_to(WEDNESDAY_NIGHT)
    await events_until(session, agent_exited("set-mutation"))
    assert steps.runs == ["Mutation tests"]

    await clock.go_to(THURSDAY_NIGHT)
    await events_until(session, agent_exited("set-mutation"))
    await session.close()
    assert steps.runs == ["Mutation tests", "Mutation tests"]


async def test_two_workspaces_fire_their_own_schedules_and_closing_one_stops_its_own() -> None:
    first_clock = FakeScheduleClock(TUESDAY_MORNING)
    second_clock = FakeScheduleClock(TUESDAY_MORNING)
    first, _ = opened(first_clock, FakeStore(), tidy=TIDY)
    second, second_store = opened(
        second_clock, FakeStore(), late=TIDY.model_copy(update={"cron": "0 4 * * *"})
    )
    for session, clock in ((first, first_clock), (second, second_clock)):
        await greeted(session)
        await clock.parked()

    await first_clock.go_to(WEDNESDAY_NIGHT)
    await second_clock.go_to(WEDNESDAY_NIGHT)
    assert shown(first) == [("tidy", "Tidy up after tidy")]
    assert shown(second) == []

    await first.close()
    sleeps = len(first_clock.sleeps)
    await second_clock.go_to(WEDNESDAY_NIGHT + timedelta(hours=1))
    assert not schedules_of(first).running
    assert len(first_clock.sleeps) == sleeps
    assert schedules_of(second).running
    assert shown(second) == [("late", "Tidy up after late")]
    assert second_store.fired == {"late": WEDNESDAY_NIGHT + timedelta(hours=1)}
    await second.close()
