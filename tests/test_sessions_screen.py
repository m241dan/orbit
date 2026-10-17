"""Picking a session: the modal shown at startup and on ctrl+p, opening a saved session or
starting a new one, and renaming or deleting saved ones."""

from datetime import UTC, datetime

from textual.containers import Vertical
from textual.widgets import Input, OptionList

from orbit.agent import AgentSession
from orbit.protocol import (
    DeleteSession,
    ErrorOccurred,
    ListSessions,
    NewSession,
    OpenSession,
    RenameSession,
    SessionOpened,
    SessionsListed,
    SessionSummary,
)
from orbit.tui import OrbitApp
from orbit.tui.sessions import SessionsScreen
from orbit.tui.widgets import ConfirmScreen
from tests.test_session_switch import saved, store_with
from tests.test_tui import FakeSession, entries, feed_events

ACTIVE = datetime(2026, 10, 16, 18, 2, tzinfo=UTC)
OLD = SessionSummary(id="old", name="auth rework", last_active=ACTIVE)
CURRENT = SessionSummary(id="now", name="today", last_active=ACTIVE, current=True)
LOCKED = SessionSummary(id="busy", name="elsewhere", locked=True)
BROKEN = SessionSummary(id="bad", name="bad.json", unreadable="not JSON")
LISTED = SessionsListed(sessions=(CURRENT, OLD, LOCKED, BROKEN), reason="startup")
ASKED = LISTED.model_copy(update={"reason": "asked"})
REFRESHED = LISTED.model_copy(update={"reason": "refreshed"})


def options(screen: SessionsScreen) -> list[str]:
    option_list = screen.query_one(OptionList)
    return [str(option_list.get_option_at_index(i).prompt) for i in range(option_list.option_count)]


async def test_sessions_listed_at_startup_show_the_modal_with_new_session_first() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, SessionsListed(sessions=(OLD,), reason="startup"))

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
        await feed_events(session, pilot, ASKED)

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
        await feed_events(session, pilot, ASKED)
        await pilot.press("escape")

        assert not isinstance(app.screen, SessionsScreen)
        assert session.commands == [ListSessions()]
        assert app.is_running


async def test_sessions_listed_unasked_after_ctrl_p_open_no_picker() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        await feed_events(session, pilot, ASKED)
        await pilot.press("escape")
        await feed_events(session, pilot, REFRESHED)

        assert not any(isinstance(s, SessionsScreen) for s in app.screen_stack)
        assert app.is_running


async def test_a_ctrl_p_answer_after_the_startup_list_closed_opens_one_escape_closes() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        await feed_events(session, pilot, LISTED)
        await pilot.press("j", "enter")
        await feed_events(session, pilot, ASKED)
        screen = app.screen
        assert isinstance(screen, SessionsScreen)
        assert not screen.startup
        await pilot.press("escape")
        await pilot.pause()

        assert not any(isinstance(s, SessionsScreen) for s in app.screen_stack)
        assert app.is_running


async def test_a_refreshed_listing_with_no_list_shown_opens_none() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, REFRESHED)
        await pilot.press("escape")
        await pilot.pause()

        assert len(app.screen_stack) == 1
        assert app.is_running


async def test_the_listing_after_a_delete_opens_no_picker_once_closed() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        await feed_events(session, pilot, ASKED)
        await pilot.press("j", "j", "x", "y", "escape")
        await feed_events(session, pilot, REFRESHED)
        await pilot.press("escape")
        await pilot.pause()

        assert session.commands == [ListSessions(), DeleteSession(session_id="old")]
        assert not any(isinstance(s, SessionsScreen) for s in app.screen_stack)
        assert app.is_running


async def test_the_listing_after_a_rename_opens_no_picker_once_closed() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        await feed_events(session, pilot, ASKED)
        await pilot.press("j", "j", "r", "ctrl+u", *"jk", "enter", "escape")
        await feed_events(session, pilot, REFRESHED)
        await pilot.press("escape")
        await pilot.pause()

        assert session.commands == [ListSessions(), RenameSession(session_id="old", name="jk")]
        assert not any(isinstance(s, SessionsScreen) for s in app.screen_stack)
        assert app.is_running


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


def subtitle(screen: SessionsScreen) -> str:
    return str(screen.query_one(Vertical).border_subtitle)


async def test_r_asks_for_the_highlighted_sessions_new_name_starting_from_its_name() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, LISTED)
        await pilot.press("j", "j", "r")

        screen = app.screen
        assert isinstance(screen, SessionsScreen)
        name = screen.query_one(Input)
        assert name.display and app.focused is name
        assert name.value == "auth rework"
        assert subtitle(screen) == "enter rename · esc back"
        await pilot.press("ctrl+u", *"jk", "enter")

        assert session.commands == [RenameSession(session_id="old", name="jk")]
        assert app.screen is screen
        assert not name.display
        assert app.focused is screen.query_one(OptionList)
        assert subtitle(screen) == "j/k move · enter open · r rename · x delete · esc quit"


async def test_the_current_session_can_be_renamed() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, LISTED)
        await pilot.press("j", "r", "!", "enter")
        assert session.commands == [RenameSession(session_id="now", name="today!")]


async def test_an_empty_new_name_keeps_the_old_one() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, LISTED)
        await pilot.press("j", "r", "ctrl+u", "space", "enter")

        assert session.commands == []
        assert isinstance(app.screen, SessionsScreen)
        assert not app.screen.query_one(Input).display


async def test_escape_from_a_new_name_goes_back_to_the_list_without_renaming() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, LISTED)
        await pilot.press("j", "r", "a", "escape")

        screen = app.screen
        assert isinstance(screen, SessionsScreen)
        assert not screen.query_one(Input).display
        await pilot.press("k", "enter")  # Back on the list: a new session, not a rename.
        await pilot.press("enter")
        assert session.commands == [NewSession(name=None)]


async def test_r_and_x_do_nothing_on_new_session() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, LISTED)
        await pilot.press("r", "x")

        screen = app.screen
        assert isinstance(screen, SessionsScreen)
        assert not screen.query_one(Input).display
        assert session.commands == []


async def test_x_deletes_the_highlighted_session_once_confirmed() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, LISTED)
        await pilot.press("j", "j", "x")

        confirm = app.screen
        assert isinstance(confirm, ConfirmScreen)
        assert confirm.title_text == "Delete session"
        assert confirm.message == (
            'Delete "auth rework"? Its tasks and review state are gone for good.'
        )
        await pilot.press("y")

        assert session.commands == [DeleteSession(session_id="old")]
        assert isinstance(app.screen, SessionsScreen)


async def test_n_or_escape_keeps_the_session() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, LISTED)
        await pilot.press("j", "j", "x", "n", "x", "escape")

        assert session.commands == []
        assert isinstance(app.screen, SessionsScreen)


async def test_deleting_a_session_open_somewhere_is_left_to_the_backend_to_refuse() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, LISTED)
        await pilot.press("j", "x", "j", "j", "x")

        assert session.commands == [
            DeleteSession(session_id="now"),
            DeleteSession(session_id="busy"),
        ]
        assert isinstance(app.screen, SessionsScreen)


async def test_sessions_listed_again_refresh_the_list_shown_in_place() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, LISTED)
        screen = app.screen
        assert isinstance(screen, SessionsScreen)
        await pilot.press("j", "j", "j")
        renamed = OLD.model_copy(update={"name": "flags"})
        refreshed = SessionsListed(sessions=(CURRENT, renamed), reason="refreshed")
        await feed_events(session, pilot, refreshed)

        assert screen is app.screen and isinstance(screen, SessionsScreen)
        assert screen.sessions == (CURRENT, renamed)
        assert options(screen) == [
            "+ new session",
            "● today · Oct 16 18:02",
            "  flags · Oct 16 18:02",
        ]
        assert screen.query_one(OptionList).highlighted == 2  # Kept within the list.
        await pilot.press("enter")
        assert session.commands == [OpenSession(session_id="old")]


async def test_renaming_and_deleting_through_the_backend_refresh_the_list() -> None:
    store = store_with(saved("old"), saved("gone"))
    backend = AgentSession(store=store, pick_session=True)
    app = OrbitApp(backend)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SessionsScreen)
        await pilot.press("j", "x", "y")
        await pilot.pause()
        await pilot.press("r", "ctrl+u", *"new", "enter")
        await pilot.pause()

        assert screen is app.screen and isinstance(screen, SessionsScreen)
        assert [(s.id, s.name) for s in screen.sessions] == [("old", "new")]
        assert store.deleted == ["gone"]


async def test_ctrl_p_twice_shows_one_list_that_escape_closes() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p", "ctrl+p")
        await feed_events(session, pilot, ASKED, ASKED)

        shown = [s for s in app.screen_stack if isinstance(s, SessionsScreen)]
        assert len(shown) == 1
        assert not shown[0].startup
        await pilot.press("escape")
        await pilot.pause()
        assert app.is_running
        assert len(app.screen_stack) == 1


async def test_a_listing_answering_ctrl_p_is_never_at_startup() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p", "ctrl+p")
        await feed_events(session, pilot, ASKED)
        await pilot.press("escape")
        await feed_events(session, pilot, ASKED)

        screen = app.screen
        assert isinstance(screen, SessionsScreen)
        assert not screen.startup


async def test_a_late_listing_after_the_picker_closed_opens_no_picker() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, LISTED)
        await pilot.press("j", "j", "j", "x", "k", "enter")
        await feed_events(session, pilot, REFRESHED)

        assert session.commands == [DeleteSession(session_id="busy"), OpenSession(session_id="old")]
        assert not any(isinstance(s, SessionsScreen) for s in app.screen_stack)
        assert app.is_running


async def test_ctrl_p_after_the_startup_picker_opens_a_picker_again() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, LISTED)
        await pilot.press("j", "enter", "ctrl+p")
        await feed_events(session, pilot, ASKED)

        screen = app.screen
        assert isinstance(screen, SessionsScreen)
        assert not screen.startup
