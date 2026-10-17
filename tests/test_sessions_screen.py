"""Picking a session: the modal shown at startup and on ctrl+p, opening a saved session or
starting a new one."""

from datetime import UTC, datetime

from textual.widgets import Input, OptionList

from orbit.agent import AgentSession
from orbit.protocol import (
    ErrorOccurred,
    ListSessions,
    NewSession,
    OpenSession,
    SessionOpened,
    SessionsListed,
    SessionSummary,
)
from orbit.tui import OrbitApp
from orbit.tui.sessions import SessionsScreen
from tests.test_session_switch import saved, store_with
from tests.test_tui import FakeSession, entries, feed_events

ACTIVE = datetime(2026, 10, 16, 18, 2, tzinfo=UTC)
OLD = SessionSummary(id="old", name="auth rework", last_active=ACTIVE)
CURRENT = SessionSummary(id="now", name="today", last_active=ACTIVE, current=True)
LOCKED = SessionSummary(id="busy", name="elsewhere", locked=True)
BROKEN = SessionSummary(id="bad", name="bad.json", unreadable="not JSON")
LISTED = SessionsListed(sessions=(CURRENT, OLD, LOCKED, BROKEN))


def options(screen: SessionsScreen) -> list[str]:
    option_list = screen.query_one(OptionList)
    return [str(option_list.get_option_at_index(i).prompt) for i in range(option_list.option_count)]


async def test_sessions_listed_at_startup_show_the_modal_with_new_session_first() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, SessionsListed(sessions=(OLD,)))

        screen = app.screen
        assert isinstance(screen, SessionsScreen)
        assert screen.startup
        assert options(screen) == ["+ new session", "  auth rework · Oct 16 18:02"]
        assert screen.query_one(OptionList).highlighted == 0


async def test_a_backend_picking_at_startup_shows_the_modal() -> None:
    backend = AgentSession(store=store_with(saved("old")), pick_session=True)
    app = OrbitApp(backend)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SessionsScreen)
        assert [s.id for s in screen.sessions] == ["old"]


async def test_a_backend_starting_a_new_session_shows_no_modal() -> None:
    backend = AgentSession(store=store_with(saved("old")), name="auth rework")
    app = OrbitApp(backend)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not isinstance(app.screen, SessionsScreen)
        assert backend.saved_session.name == "auth rework"


async def test_enter_opens_the_highlighted_session() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, LISTED)
        await pilot.press("j", "j", "k", "down", "enter")

        assert session.commands == [OpenSession(session_id="old")]
        assert not isinstance(app.screen, SessionsScreen)


async def test_choosing_the_current_session_just_closes_the_modal() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, LISTED)
        await pilot.press("j", "enter")

        assert session.commands == []
        assert not isinstance(app.screen, SessionsScreen)


async def test_locked_and_unreadable_sessions_cannot_be_opened() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, LISTED)
        screen = app.screen
        assert isinstance(screen, SessionsScreen)
        assert options(screen)[3:] == [
            "  elsewhere · open in another orbit",
            "  bad.json · unreadable: not JSON",
        ]
        await pilot.press("j", "j", "j", "enter", "j", "enter")

        assert session.commands == []
        assert app.screen is screen


async def test_a_new_session_is_started_with_the_name_typed() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, LISTED)
        await pilot.press("enter")
        name = app.screen.query_one(Input)
        assert name.display
        assert app.focused is name
        await pilot.press(*" jk ", "enter")

        assert session.commands == [NewSession(name="jk")]
        assert not isinstance(app.screen, SessionsScreen)


async def test_a_new_session_without_a_name_leaves_it_to_the_backend() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, LISTED)
        await pilot.press("enter", "space", "enter")

        assert session.commands == [NewSession(name=None)]


async def test_escape_from_the_name_goes_back_to_the_list() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, LISTED)
        await pilot.press("enter", "a", "escape")

        screen = app.screen
        assert isinstance(screen, SessionsScreen)
        name = screen.query_one(Input)
        assert not name.display
        assert name.value == ""
        assert app.focused is screen.query_one(OptionList)
        await pilot.press("j", "enter")
        assert session.commands == []  # Back on the list, j moved to the current session.


async def test_escape_at_startup_quits_orbit() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, LISTED)
        await pilot.press("escape")
        await pilot.pause()
        assert not app.is_running
    assert session.commands == []


async def test_ctrl_p_lists_the_sessions_in_the_modal() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        assert session.commands == [ListSessions()]
        await feed_events(session, pilot, LISTED)

        screen = app.screen
        assert isinstance(screen, SessionsScreen)
        assert not screen.startup
        await pilot.press("j", "j", "enter")
        assert session.commands == [ListSessions(), OpenSession(session_id="old")]


async def test_escape_mid_session_keeps_the_current_session() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        await feed_events(session, pilot, LISTED)
        await pilot.press("escape")

        assert not isinstance(app.screen, SessionsScreen)
        assert session.commands == [ListSessions()]
        assert app.is_running


async def test_sessions_listed_unasked_after_ctrl_p_are_at_startup_again() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        await feed_events(session, pilot, LISTED)
        await pilot.press("escape")
        await feed_events(session, pilot, LISTED)

        screen = app.screen
        assert isinstance(screen, SessionsScreen)
        assert screen.startup


async def test_ctrl_p_does_nothing_over_another_modal() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, LISTED)
        await pilot.press("ctrl+p")
        assert session.commands == []


async def test_a_refusal_shows_as_an_error_once_the_modal_closed() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, LISTED)
        await pilot.press("j", "j", "enter")
        await feed_events(session, pilot, ErrorOccurred(message="Tasks are running."))

        assert not isinstance(app.screen, SessionsScreen)
        assert entries(app) == [("Tasks are running.", "error")]


async def test_a_session_opened_adds_nothing_to_the_transcript() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, SessionOpened(session=OLD))
        assert entries(app) == []
