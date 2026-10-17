"""Tests for the session switcher on its own, in a session without agents."""

import asyncio
from datetime import UTC, datetime
from itertools import count

from orbit.agent.application.keeper import SessionKeeper
from orbit.agent.application.planner_seat import PlannerSeat, SeatHooks
from orbit.agent.application.switcher import (
    TASKS_RUNNING,
    SessionSwitcher,
    SettingsAccess,
    SwitchParts,
)
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.reviews import LaneSnapshot, PlannerSnapshot
from orbit.agent.domain.sessions import SessionRecord
from orbit.protocol import (
    ErrorOccurred,
    Event,
    NewSession,
    OpenSession,
    SessionOpened,
    Settings,
    SettingsChanged,
    TasksChanged,
    TaskSpec,
    Usage,
)
from tests.agent_fakes import FakeStore, Ticks

EARLIER = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
REVIEWS = LaneSnapshot(planner=PlannerSnapshot(paused=True))


class Held:
    """A session's settings and whether its tasks run, as the switcher sees them."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.running = False

    def set(self, settings: Settings) -> None:
        self.settings = settings

    def changed(self) -> SettingsChanged:
        return SettingsChanged(settings=self.settings)


async def nothing() -> None:
    return None


def switcher_for(
    store: FakeStore, held: Held, *, picking: bool = False, pinned: tuple[str, ...] = ()
) -> tuple[SessionSwitcher, "asyncio.Queue[Event]", Tasks]:
    """A switcher of a session without agents saved in ``store``, its settings ``held``."""
    events: asyncio.Queue[Event] = asyncio.Queue()
    ids = (f"session-{n}" for n in count(1))
    keeper = SessionKeeper(store, events.put, Ticks(), lambda: next(ids))
    tasks = Tasks(TaskBoard(), events.put)
    seat = PlannerSeat(None, tasks, events, SeatHooks(nothing, keeper.use, tuple))
    parts = SwitchParts(keeper, tasks, seat, None, None, lambda: held.running)
    access = SettingsAccess(lambda: held.settings, held.set, held.changed, held.settings, pinned)
    return SessionSwitcher(parts, access, events, picking), events, tasks


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
    held.running = True
    switcher, events, _ = switcher_for(FakeStore(), held)
    await switcher.handle(NewSession(name="other"))
    assert drained(events) == [ErrorOccurred(message=TASKS_RUNNING)]


async def test_a_switch_publishes_the_tasks_then_the_settings_then_the_session() -> None:
    switcher, events, _ = switcher_for(FakeStore(), Held(Settings()))
    await switcher.handle(NewSession(name="other"))
    kinds = [type(e) for e in drained(events)]
    assert kinds == [TasksChanged, SettingsChanged, SessionOpened]


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
    await switcher.handle(OpenSession(session_id="auth"))
    assert held.settings == Settings(model="opus", tool_access="full")


async def test_without_a_review_lane_the_opened_reviews_are_saved_back() -> None:
    store = FakeStore()
    store.saved = [saved(reviews=REVIEWS, spent=Usage(cost_usd=1.5))]
    switcher, _, _ = switcher_for(store, Held(Settings()))
    await switcher.handle(OpenSession(session_id="auth"))
    contents = switcher.contents()
    assert contents.reviews == REVIEWS and contents.spent == Usage(cost_usd=1.5)


async def test_tasks_added_before_a_session_is_picked_carry_over() -> None:
    store = FakeStore()
    store.saved = [saved()]
    switcher, _, tasks = switcher_for(store, Held(Settings()), picking=True)
    await tasks.change(lambda board: board.add(TaskSpec(prompt="Fix it.")))
    await switcher.handle(OpenSession(session_id="auth"))
    assert [t.task.prompt for t in tasks.board.tasks] == ["Fix it."]


async def test_once_the_user_stays_added_tasks_dont_carry_over() -> None:
    store = FakeStore()
    store.saved = [saved()]
    switcher, _, tasks = switcher_for(store, Held(Settings()), picking=True)
    await tasks.change(lambda board: board.add(TaskSpec(prompt="Fix it.")))
    switcher.stay()
    await switcher.handle(OpenSession(session_id="auth"))
    assert tasks.board.tasks == ()


async def test_contents_hold_the_board_and_settings() -> None:
    held = Held(Settings(model="opus"))
    switcher, _, tasks = switcher_for(FakeStore(), held)
    await tasks.change(lambda board: board.add(TaskSpec(prompt="Fix it.")))
    contents = switcher.contents()
    assert contents.settings == Settings(model="opus")
    assert [t.task.prompt for t in contents.tasks] == ["Fix it."]
