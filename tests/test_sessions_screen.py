"""Picking a session: the picker shown at startup and on ctrl+p, viewing or activating a saved
session or starting a new one, and renaming or deleting saved ones."""

from textual.containers import Vertical
from textual.widgets import Input, OptionList

from orbit.agent import WorkspaceHost
from orbit.protocol import (
    BUSY,
    Attach,
    CommandRefused,
    DeleteSession,
    ErrorOccurred,
    NewSession,
    RenameSession,
    SessionSummary,
    Usage,
)
from orbit.tui import Opening, OrbitApp
from orbit.tui.sessions import SessionsScreen
from orbit.tui.tasks import TaskList
from orbit.tui.transcript import Transcript
from orbit.tui.widgets import ConfirmScreen
from tests.agent_fakes import frontend
from tests.session_apps import (
    BROKEN,
    CATALOG,
    CURRENT,
    LOCKED,
    OLD,
    RUNNING,
    SWITCH_TO_OLD,
    catalog,
    in_process,
    joined,
    started,
)
from tests.session_builders import saved, store_with
from tests.tui_fakes import FakeSession, changes, entries, feed_events, opened

OLD_ROW = "  auth rework · Oct 16 18:02"
SAVED_ROWS = [OLD_ROW, "  elsewhere · open in another orbit", "  bad.json · unreadable: not JSON"]


def options(screen: SessionsScreen) -> list[str]:
    option_list = screen.query_one(OptionList)
    return [str(option_list.get_option_at_index(i).prompt) for i in range(option_list.option_count)]


def picker(app: OrbitApp) -> SessionsScreen:
    screen = app.screen
    assert isinstance(screen, SessionsScreen)
    return screen


def no_picker(app: OrbitApp) -> bool:
    return not any(isinstance(screen, SessionsScreen) for screen in app.screen_stack)


async def test_in_process_the_startup_picker_lists_the_saved_sessions_then_new_session() -> None:
    app, session = in_process()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG)

        screen = picker(app)
        assert screen.startup and not screen.resume
        assert options(screen) == [*SAVED_ROWS, "New session"]
        assert screen.query_one(OptionList).highlighted == 0
        assert changes(session) == []


async def test_connected_the_startup_picker_offers_to_resume_the_active_session_first() -> None:
    app, session = joined()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG)
        await pilot.press("enter")

        screen = picker(app)
        assert options(screen) == [
            "  Resume today · Oct 16 18:02 · active · viewed by 1",
            *SAVED_ROWS,
            "New session",
        ]
        assert screen.query_one(OptionList).highlighted == 0
        await pilot.press("enter")
        assert changes(session) == [Attach(workspace="w", session="now")]
        assert no_picker(app)


async def test_a_backend_picking_at_startup_shows_the_picker_without_its_new_session() -> None:
    backend = WorkspaceHost(store=store_with(saved("old")), pick_session=True)
    app = OrbitApp(frontend(backend), opening=Opening(pick=True))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert [s.id for s in picker(app).rows] == ["old"]


async def test_an_app_not_picking_shows_no_picker_and_sends_no_listing() -> None:
    backend = WorkspaceHost(store=store_with(saved("old")), name="auth rework")
    app = OrbitApp(frontend(backend))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert no_picker(app)
        assert backend.saved_session.name == "auth rework"


async def test_picking_with_the_active_session_idle_and_unviewed_activates_it() -> None:
    app, session = in_process()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG)
        await pilot.press("j", "k", "enter")

        assert changes(session) == SWITCH_TO_OLD
        assert no_picker(app)


async def test_a_state_change_updates_the_row_while_the_picker_is_open() -> None:
    app, session = joined()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG)
        await pilot.press("enter")
        await feed_events(session, pilot, RUNNING)
        assert options(picker(app))[0] == (
            "  Resume today · Oct 16 18:02 · active · running · viewed by 1"
        )


async def test_picking_a_session_that_fails_to_send_shows_why_and_activates_nothing() -> None:
    app, session = in_process()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG)
        session.send_error = RuntimeError("the backend is gone")
        await pilot.press("enter")
        await pilot.pause()

        assert app.is_running
        assert changes(session) == [Attach(workspace="w", session="old")]
        assert picker(app).note == "Couldn't send the command: the backend is gone"
        assert entries(app) == []


async def test_a_new_session_is_started_with_the_name_typed() -> None:
    app, session = in_process()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG)
        await pilot.press("j", "j", "j", "enter")
        name = app.screen.query_one(Input)
        assert name.display
        assert app.focused is name
        await pilot.press(*" jk ", "enter")

        assert changes(session) == [NewSession(name="jk")]
        assert no_picker(app)


async def test_escape_from_the_name_goes_back_to_the_list() -> None:
    app, session = in_process()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, catalog(CURRENT, LOCKED))
        await pilot.press("j", "enter", "a", "escape")

        screen = picker(app)
        name = screen.query_one(Input)
        assert not name.display
        assert name.value == ""
        assert app.focused is screen.query_one(OptionList)
        await pilot.press("k", "enter")
        assert changes(session) == []  # Back on the list, k moved to the locked session.


async def test_escape_at_startup_quits_orbit() -> None:
    app, session = in_process()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG)
        await pilot.press("escape")
        await pilot.pause()
        assert not app.is_running
    assert changes(session) == []


async def test_ctrl_p_shows_the_picker_with_the_session_viewed_first() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG, opened(CURRENT))
        await pilot.press("ctrl+p")

        screen = picker(app)
        assert not screen.startup
        assert options(screen)[:2] == ["● today · Oct 16 18:02 · active", OLD_ROW]
        await pilot.press("j", "enter")
        assert changes(session) == SWITCH_TO_OLD


async def test_only_the_first_catalog_opens_the_startup_picker() -> None:
    app, session = in_process()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG)
        await pilot.press("enter")
        await feed_events(session, pilot, CATALOG)

        assert changes(session) == SWITCH_TO_OLD
        assert no_picker(app)
        assert app.is_running


async def test_a_catalog_opens_no_picker_in_an_app_not_picking() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG)
        assert no_picker(app)


async def test_ctrl_p_does_nothing_over_another_modal() -> None:
    app, session = in_process()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG)
        screen = picker(app)
        await pilot.press("ctrl+p")
        assert app.screen is screen
        assert changes(session) == []


async def test_ctrl_p_twice_shows_one_picker_that_escape_closes() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG)
        app.action_list_sessions()
        app.action_list_sessions()
        await pilot.pause()

        assert len([s for s in app.screen_stack if isinstance(s, SessionsScreen)]) == 1
        await pilot.press("escape")
        await pilot.pause()
        assert app.is_running
        assert len(app.screen_stack) == 1


async def test_ctrl_p_after_the_startup_picker_opens_a_picker_again() -> None:
    app, session = joined()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG)
        await pilot.press("enter", "enter", "ctrl+p")

        screen = picker(app)
        assert not screen.startup
        assert options(screen)[0] == "● today · Oct 16 18:02 · active"


async def test_a_refusal_shows_as_an_error_once_the_picker_closed() -> None:
    app, session = in_process()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG)
        await pilot.press("enter")
        await feed_events(session, pilot, ErrorOccurred(code=BUSY, message="Tasks are running."))

        assert no_picker(app)
        assert entries(app) == [("Tasks are running.", "error")]


async def test_a_snapshot_of_a_session_opened_adds_nothing_to_the_transcript() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, opened(OLD))
        assert entries(app) == []


def subtitle(screen: SessionsScreen) -> str:
    return str(screen.query_one(Vertical).border_subtitle)


async def test_r_asks_for_the_highlighted_sessions_new_name_starting_from_its_name() -> None:
    app, session = in_process()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG)
        await pilot.press("r")

        screen = picker(app)
        name = screen.query_one(Input)
        assert name.display and app.focused is name
        assert name.value == "auth rework"
        assert subtitle(screen) == "enter rename · esc back"
        await pilot.press("ctrl+u", *"jk", "enter")

        assert changes(session) == [RenameSession(session_id="old", name="jk")]
        assert app.screen is screen
        assert not name.display
        assert app.focused is screen.query_one(OptionList)
        assert subtitle(screen) == "j/k move · enter open · r rename · x delete · esc quit"


async def test_x_deletes_the_highlighted_session_once_confirmed() -> None:
    app, session = in_process()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG)
        await pilot.press("x")

        confirm = app.screen
        assert isinstance(confirm, ConfirmScreen)
        assert confirm.title_text == "Delete session"
        assert confirm.message == (
            'Delete "auth rework"? Its tasks and review state are gone for good.'
        )
        await pilot.press("y")

        assert changes(session) == [DeleteSession(session_id="old")]
        assert isinstance(app.screen, SessionsScreen)


def refused(session: FakeSession, message: str) -> CommandRefused:
    """The backend refusing the last change ``session`` got, saying ``message``."""
    return CommandRefused(command_id=session.envelopes[-1].command_id, code=BUSY, message=message)


CANT_DELETE = "Can't delete today: it's the active session."


async def test_a_refused_delete_shows_why_under_the_list_and_not_in_the_conversation() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG)
        await pilot.press("ctrl+p", "x")
        await feed_events(session, pilot, refused(session, CANT_DELETE))

        screen = picker(app)
        assert app.screen is screen
        assert screen.note == CANT_DELETE
        assert entries(app) == []


async def test_a_refusal_once_the_picker_closed_shows_nowhere() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG)
        await pilot.press("ctrl+p", "x", "escape")
        await feed_events(session, pilot, refused(session, CANT_DELETE))

        assert no_picker(app)
        assert entries(app) == []
        assert app.is_running


CANT_SWITCH = "Can't switch: tasks are running."


async def test_a_refused_pick_shows_why_in_the_picker_and_not_in_the_conversation() -> None:
    app, session = in_process()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG)
        await pilot.press("enter")
        await feed_events(session, pilot, refused(session, CANT_SWITCH))

        screen = picker(app)
        assert changes(session) == SWITCH_TO_OLD
        assert screen.note == CANT_SWITCH
        assert screen.startup
        assert entries(app) == []


async def test_each_refusal_of_a_pick_shows_in_the_one_picker_shown_again() -> None:
    app, session = in_process()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG)
        await pilot.press("enter")
        attach, activate = session.envelopes[-2:]
        await feed_events(
            session, pilot, CommandRefused(command_id=attach.command_id, code=BUSY, message="a")
        )
        await feed_events(
            session, pilot, CommandRefused(command_id=activate.command_id, code=BUSY, message="b")
        )

        assert picker(app).note == "b"
        assert len([s for s in app.screen_stack if isinstance(s, SessionsScreen)]) == 1
        assert entries(app) == []


async def test_renaming_and_deleting_through_the_backend_refresh_the_picker() -> None:
    store = store_with(saved("old"), saved("gone"))
    backend = WorkspaceHost(store=store, pick_session=True)
    app = OrbitApp(frontend(backend), opening=Opening(pick=True))
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = picker(app)
        await pilot.press("x", "y")
        await pilot.pause()
        await pilot.press("r", "ctrl+u", *"new", "enter")
        await pilot.pause()

        assert screen is app.screen
        assert [(s.id, s.name) for s in screen.rows] == [("old", "new")]
        assert store.deleted == ["gone"]


def conversation_title(app: OrbitApp) -> str:
    return str(app.query_one(Transcript).border_title)


async def test_the_conversation_title_is_plain_before_any_session_event() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test():
        assert conversation_title(app) == "Conversation"


async def test_a_session_opened_names_the_conversation_through_tabs_and_hidden_thinking() -> None:
    app, session = started()
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(session, pilot, opened(OLD))
        assert conversation_title(app) == "Conversation — auth rework"
        await pilot.press("tab", "tab", "tab", "t")  # coder
        assert conversation_title(app) == "Conversation · coder · thinking hidden — auth rework"


async def test_a_catalog_renaming_the_session_viewed_renames_the_conversation() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        spent = Usage(input_tokens=2_000, cost_usd=2.0)
        await feed_events(session, pilot, opened(OLD.model_copy(update={"usage": spent})))
        renamed = OLD.model_copy(update={"name": "flags"})
        await feed_events(session, pilot, catalog(CURRENT, renamed))
        assert no_picker(app)
        assert conversation_title(app) == "Conversation — flags"
        assert app.query_one(TaskList).border_subtitle == "session: 2k tok · $2.00"


async def test_a_catalog_without_the_session_viewed_keeps_the_conversation_title() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, opened(OLD))
        await feed_events(session, pilot, catalog(CURRENT))
        assert conversation_title(app) == "Conversation — auth rework"


async def test_a_pick_takes_the_session_picked_though_the_list_changed_before_it_is_taken() -> None:
    app, session = in_process()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG)
        screen = picker(app)
        screen.query_one(OptionList).action_select()
        screen.picker.show(
            (CURRENT, LOCKED, OLD, BROKEN), "now"
        )  # Puts the locked one where OLD was.
        await pilot.pause()
        assert changes(session) == SWITCH_TO_OLD


async def test_a_pick_whose_session_left_the_list_before_it_is_taken_does_nothing() -> None:
    app, session = in_process()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG)
        screen = picker(app)
        screen.query_one(OptionList).action_select()
        other = SessionSummary(id="other", name="other")
        screen.picker.show((CURRENT, other), "now")  # Puts another session where OLD was.
        await pilot.pause()
        assert app.screen is screen
        assert changes(session) == []
