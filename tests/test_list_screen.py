"""The shared Vim-style movement of the list modals, and the status lines by the prompt."""

from typing import Any

from textual.app import App, ComposeResult
from textual.widgets import Input, OptionList, Static

from orbit.protocol import SessionSummary
from orbit.tui.sessions import ActivationScreen, SessionChange, SessionsScreen
from orbit.tui.widgets import ListScreen, PickerScreen, show_status

SESSIONS = (SessionSummary(id="a", name="first"), SessionSummary(id="b", name="second"))


async def ignore(change: SessionChange) -> None:
    del change


def screens() -> list[ListScreen[Any]]:
    """One of each list modal, with at least three rows."""
    return [
        PickerScreen("Pick", ["one", "two", "three"]),
        SessionsScreen(SESSIONS, None, ignore),
        ActivationScreen("Look or activate?", ["one", "two", "three"]),
    ]


async def test_j_and_k_move_the_highlight_in_each_list_modal() -> None:
    for screen in screens():
        app: App[None] = App()
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()
            options = screen.query_one(OptionList)
            options.highlighted = 0
            await pilot.press("j", "j")
            assert options.highlighted == 2
            await pilot.press("k")
            assert options.highlighted == 1


async def test_j_and_k_type_in_the_session_name_box() -> None:
    screen = SessionsScreen(SESSIONS, None, ignore)
    app: App[None] = App()
    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()
        await pilot.press("j", "j", "enter", "j", "k")  # Onto New session, then name it.
        assert screen.query_one(Input).value == "jk"
        assert screen.query_one(OptionList).highlighted == 2


class Lines(App[None]):
    def compose(self) -> ComposeResult:
        yield Static("old", id="line")


async def test_a_status_shows_its_line_and_none_hides_it() -> None:
    app = Lines()
    async with app.run_test():
        line = app.query_one("#line", Static)
        show_status(line, "halted")
        assert str(line.render()) == "halted"
        assert line.display
        show_status(line, None)
        assert str(line.render()) == ""
        assert not line.display
