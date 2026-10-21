"""The shared Vim-style movement of the list modals, and the status lines by the prompt."""

from typing import Any

from textual.app import App, ComposeResult
from textual.color import Color
from textual.widgets import Input, OptionList

from orbit.protocol import SessionSummary
from orbit.tui.sessions import ActivationScreen, SessionChange, SessionsScreen
from orbit.tui.view.sessions import Viewing
from orbit.tui.widgets import ListScreen, PickerScreen, StatusLine

SESSIONS = (SessionSummary(id="a", name="first"), SessionSummary(id="b", name="second"))


async def ignore(change: SessionChange) -> None:
    del change


def screens() -> list[ListScreen[Any]]:
    """One of each list modal, with at least three rows."""
    return [
        PickerScreen("Pick", ["one", "two", "three"]),
        SessionsScreen(SESSIONS, Viewing(workspace="w", attached=None), ignore),
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
    screen = SessionsScreen(SESSIONS, Viewing(workspace="w", attached=None), ignore)
    app: App[None] = App()
    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()
        await pilot.press("j", "j", "enter", "j", "k")  # Onto New session, then name it.
        assert screen.query_one(Input).value == "jk"
        assert screen.query_one(OptionList).highlighted == 2


class Lines(App[None]):
    def compose(self) -> ComposeResult:
        yield StatusLine("old", id="line")


async def test_a_status_line_shows_its_status_and_none_hides_it() -> None:
    app = Lines()
    async with app.run_test():
        line = app.query_one("#line", StatusLine)
        line.show("halted")
        assert str(line.render()) == "halted"
        assert line.display
        line.show(None)
        assert str(line.render()) == ""
        assert not line.display


class Tones(App[None]):
    def compose(self) -> ComposeResult:
        yield StatusLine("late", id="warning")
        yield StatusLine("halted", id="error", classes="error")


async def test_a_status_line_is_a_warning_unless_marked_an_error() -> None:
    app = Tones()
    async with app.run_test():
        theme = app.get_css_variables()
        warning = app.query_one("#warning", StatusLine).styles.color
        error = app.query_one("#error", StatusLine).styles.color
        assert warning.hex == Color.parse(theme["warning"]).hex
        assert error.hex == Color.parse(theme["error"]).hex
