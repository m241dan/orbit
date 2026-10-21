"""Tests for the session switcher on its own, in a session without agents."""

import asyncio
from datetime import UTC, datetime
from itertools import count

import pytest

from orbit.agent.application.keeper import NewSessions, SessionKeeper
from orbit.agent.application.planner_seat import PlannerSeat, SeatHooks
from orbit.agent.application.settings import SessionSettings
from orbit.agent.application.switcher import SessionSwitcher, SwitchParts
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.activity import TASKS_RUNNING, Activity
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.reviews import LaneSnapshot, PlannerSnapshot
from orbit.agent.domain.sessions import SessionRecord
from orbit.protocol import (
    BUSY,
    ActivateSession,
    Catalog,
    DeleteSession,
    Event,
    NewSession,
    RenameSession,
    SessionSummary,
    Settings,
    Snapshot,
    TaskSpec,
    Usage,
)
from tests.agent_fakes import FakeHalt, FakeStore, Ticks

EARLIER = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
REVIEWS = LaneSnapshot(planner=PlannerSnapshot(paused=True))


class Held:
    """A session's settings and what's in progress there, as the switcher sees them, and when
    the sessions it was asked to snapshot were last active, and the sessions each catalog it
    was asked for holds."""

    def __init__(self, settings: Settings) -> None:
        self.owned = SessionSettings(settings)
        self.activity = Activity()
        self.switches: list[bool] = []
        self.snapshots: list[datetime | None] = []
        self.catalogs: list[tuple[SessionSummary, ...]] = []

    @property
    def settings(self) -> Settings:
        return self.owned.current

    async def snapshot(self, last_active: datetime | None) -> Snapshot:
        self.snapshots.append(last_active)
        return Snapshot(
            session=SessionSummary(id="snapshot", name="snapshot"),
            settings=self.settings,
            model_choices=(),
            busy={},
            tasks=(),
            steps=(),
            review=None,
            proposals=(),
            agents=(),
        )

    def catalog(self, sessions: tuple[SessionSummary, ...]) -> Catalog:
        self.catalogs.append(sessions)
        return Catalog(workspaces=(), agents=(), triggers=(), lanes=())


async def nothing() -> None:
    return None


def switcher_for(
    store: FakeStore, held: Held, *, picking: bool = False, pinned: tuple[str, ...] = ()
) -> tuple[SessionSwitcher, "asyncio.Queue[Event]", Tasks]:
    """A switcher of a session without agents saved in ``store``, its settings ``held``."""
    events: asyncio.Queue[Event] = asyncio.Queue()
    ids = (f"session-{n}" for n in count(1))
    keeper = SessionKeeper(store, events.put, FakeHalt(), NewSessions(Ticks(), lambda: next(ids)))
    tasks = Tasks(TaskBoard(), events.put)
    seat = PlannerSeat(None, tasks, events, SeatHooks(nothing, keeper.use, tuple))
    parts = SwitchParts(
        keeper,
        tasks,
        seat,
        None,
        None,
        lambda: held.activity,
        held.snapshot,
        held.catalog,
        events.put_nowait,
        held.switches.append,
    )
    held.owned = SessionSettings(held.owned.given, pinned)
    return SessionSwitcher(parts, held.owned, events, picking), events, tasks


def drained(events: "asyncio.Queue[Event]") -> list[Event]:
    taken: list[Event] = []
    while not events.empty():
        taken.append(events.get_nowait())
    return taken


def saved(**fields: object) -> SessionRecord:
    record = SessionRecord(id="auth", name="auth", created=EARLIER, last_active=EARLIER)
    return record.model_copy(update=fields)


async def test_switching_is_refused_while_tasks_run() -> None:
    held = Held(Settings())
    held.activity = Activity(tasks=True)
    switcher, events, _ = switcher_for(FakeStore(), held)
    with pytest.raises(Refused) as refused:
        await switcher.handle(NewSession(name="other"))
    assert (refused.value.code, str(refused.value)) == (BUSY, TASKS_RUNNING)
    assert drained(events) == []


async def test_activating_the_open_session_does_nothing() -> None:
    held = Held(Settings())
    switcher, events, _ = switcher_for(FakeStore(), held)
    await switcher.handle(ActivateSession(session="session-1"))
    assert drained(events) == []
    assert held.switches == []


async def test_a_switch_says_a_session_left_unsaved_isnt_kept() -> None:
    held = Held(Settings())
    switcher, _, _ = switcher_for(FakeStore(), held)
    await switcher.handle(NewSession(name="other"))
    assert held.switches == [False]


async def test_a_switch_says_a_saved_session_left_is_kept() -> None:
    held = Held(Settings())
    switcher, _, tasks = switcher_for(FakeStore(), held)
    await tasks.change(lambda board: board.add(TaskSpec(prompt="Add a flag.")))
    await switcher.save()
    await switcher.handle(NewSession(name="other"))
    assert held.switches == [True]


async def test_a_switch_while_picking_says_the_session_left_isnt_kept() -> None:
    held = Held(Settings())
    switcher, _, tasks = switcher_for(FakeStore(), held, picking=True)
    await tasks.change(lambda board: board.add(TaskSpec(prompt="Add a flag.")))
    await switcher.save()
    await switcher.handle(NewSession(name="other"))
    assert held.switches == [False]


async def test_a_switch_publishes_only_a_catalog_then_a_snapshot() -> None:
    held = Held(Settings())
    switcher, events, _ = switcher_for(FakeStore(), held)
    await switcher.handle(NewSession(name="other"))
    kinds = [type(e) for e in drained(events)]
    assert kinds == [Catalog, Snapshot]
    assert held.snapshots == [None]


async def test_the_catalog_after_a_new_session_lists_it_before_the_saved_ones() -> None:
    store = FakeStore()
    store.saved = [saved()]
    held = Held(Settings())
    switcher, _, _ = switcher_for(store, held)
    await switcher.handle(NewSession(name="other"))
    [sessions] = held.catalogs
    assert [(s.name, s.current) for s in sessions] == [("other", True), ("auth", False)]


async def test_the_catalog_after_opening_a_session_lists_it_as_saved() -> None:
    store = FakeStore()
    store.saved = [saved()]
    held = Held(Settings())
    switcher, _, _ = switcher_for(store, held)
    await switcher.handle(ActivateSession(session="auth"))
    [sessions] = held.catalogs
    assert [(s.name, s.current, s.last_active) for s in sessions] == [("auth", True, EARLIER)]


async def test_an_accepted_rename_publishes_a_catalog() -> None:
    held = Held(Settings())
    switcher, events, _ = switcher_for(FakeStore(), held)
    await switcher.handle(RenameSession(session_id="session-1", name="auth"))
    assert [type(e) for e in drained(events)] == [Catalog]
    assert [s.name for s in held.catalogs[0]] == ["auth"]


async def test_an_accepted_delete_publishes_a_catalog_without_the_session() -> None:
    store = FakeStore()
    store.saved = [saved()]
    held = Held(Settings())
    switcher, events, _ = switcher_for(store, held)
    await switcher.handle(DeleteSession(session_id="auth"))
    assert [type(e) for e in drained(events)] == [Catalog]
    assert [s.id for s in held.catalogs[0]] == ["session-1"]


async def test_a_refused_rename_publishes_no_catalog() -> None:
    held = Held(Settings())
    switcher, events, _ = switcher_for(FakeStore(), held)
    with pytest.raises(Refused):
        await switcher.handle(RenameSession(session_id="session-1", name=" "))
    assert drained(events) == []
    assert held.catalogs == []


async def test_an_opened_sessions_snapshot_is_as_it_was_last_active() -> None:
    store = FakeStore()
    store.saved = [saved()]
    held = Held(Settings())
    switcher, _, _ = switcher_for(store, held)
    await switcher.handle(ActivateSession(session="auth"))
    assert held.snapshots == [EARLIER]


async def test_the_summary_is_of_the_session_open_as_current() -> None:
    switcher, _, tasks = switcher_for(FakeStore(), Held(Settings()))
    await tasks.change(lambda board: board.add(TaskSpec(prompt="Fix it.")))
    switcher.spent = Usage(cost_usd=2.0)
    summary = switcher.summary()
    assert (summary.id, summary.current, summary.last_active) == ("session-1", True, None)
    assert summary.counts == {"pending": 1} and summary.usage == Usage(cost_usd=2.0)


async def test_an_opened_sessions_summary_has_what_it_spent() -> None:
    store = FakeStore()
    store.saved = [saved(spent=Usage(cost_usd=1.5))]
    switcher, _, _ = switcher_for(store, Held(Settings()))
    await switcher.handle(ActivateSession(session="auth"))
    summary = switcher.summary(EARLIER)
    assert (summary.id, summary.last_active) == ("auth", EARLIER)
    assert summary.usage == Usage(cost_usd=1.5)


async def test_a_new_session_keeps_the_settings_and_has_spent_nothing() -> None:
    held = Held(Settings(model="opus"))
    switcher, _, _ = switcher_for(FakeStore(), held)
    switcher.spent = Usage(cost_usd=2.0)
    await switcher.handle(NewSession())
    assert held.settings == Settings(model="opus")
    assert switcher.contents().spent == Usage()


async def test_an_opened_session_keeps_the_pinned_settings_as_given() -> None:
    store = FakeStore()
    store.saved = [saved(settings=Settings(model="sonnet", tool_access="full"))]
    held = Held(Settings(model="opus", tool_access="restricted"))
    switcher, _, _ = switcher_for(store, held, pinned=("model",))
    await switcher.handle(ActivateSession(session="auth"))
    assert held.settings == Settings(model="opus", tool_access="full")


async def test_without_a_review_lane_the_opened_reviews_are_saved_back() -> None:
    store = FakeStore()
    store.saved = [saved(reviews=REVIEWS, spent=Usage(cost_usd=1.5))]
    switcher, _, _ = switcher_for(store, Held(Settings()))
    await switcher.handle(ActivateSession(session="auth"))
    contents = switcher.contents()
    assert contents.reviews == REVIEWS and contents.spent == Usage(cost_usd=1.5)


async def test_tasks_added_before_a_session_is_picked_carry_over() -> None:
    store = FakeStore()
    store.saved = [saved()]
    switcher, _, tasks = switcher_for(store, Held(Settings()), picking=True)
    await tasks.change(lambda board: board.add(TaskSpec(prompt="Fix it.")))
    await switcher.handle(ActivateSession(session="auth"))
    assert [t.task.prompt for t in tasks.board.tasks] == ["Fix it."]


async def test_once_the_user_stays_added_tasks_dont_carry_over() -> None:
    store = FakeStore()
    store.saved = [saved()]
    switcher, _, tasks = switcher_for(store, Held(Settings()), picking=True)
    await tasks.change(lambda board: board.add(TaskSpec(prompt="Fix it.")))
    switcher.stay()
    await switcher.handle(ActivateSession(session="auth"))
    assert tasks.board.tasks == ()


async def test_contents_hold_the_board_and_settings() -> None:
    held = Held(Settings(model="opus"))
    switcher, _, tasks = switcher_for(FakeStore(), held)
    await tasks.change(lambda board: board.add(TaskSpec(prompt="Fix it.")))
    contents = switcher.contents()
    assert contents.settings == Settings(model="opus")
    assert [t.task.prompt for t in contents.tasks] == ["Fix it."]
