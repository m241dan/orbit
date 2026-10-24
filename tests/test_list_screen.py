"""The shared Vim-style movement of the list modals, and the status lines by the prompt."""

from typing import Any

from textual.app import App, ComposeResult
from textual.color import Color
from textual.containers import Vertical
from textual.widgets import Input, OptionList, Static

from orbit.protocol import SessionSummary
from orbit.tui.sessions import SessionChange, SessionsScreen
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.sessions import Viewing
from orbit.tui.widgets import ChoiceScreen, ListScreen, PickerScreen, StatusLine

SESSIONS = (SessionSummary(id="a", name="first"), SessionSummary(id="b", name="second"))


async def ignore(change: SessionChange, awaited: Awaited) -> None:
    del change, awaited


def screens() -> list[ListScreen[Any]]:
    """One of each list modal, with at least three rows."""
    return [
        PickerScreen("Pick", ["one", "two", "three"]),
        SessionsScreen(SESSIONS, Viewing(workspace="w", attached=None), ignore),
        ChoiceScreen("Pick", "Which?", ["one", "two", "three"], escape_index=0),
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


class Choosing(App[None]):
    """An app showing a ChoiceScreen, recording what it dismisses with."""

    def __init__(self, screen: ChoiceScreen) -> None:
        super().__init__()
        self.choice_screen = screen
        self.results: list[int | None] = []

    def on_mount(self) -> None:
        self.push_screen(self.choice_screen, self.results.append)


def choosing(*classes: str) -> Choosing:
    screen = ChoiceScreen("Pick", "Which?", ["one", "two", "three"], escape_index=2)
    return Choosing(screen.add_class(*classes))


async def test_a_choice_screen_dismisses_with_the_index_picked() -> None:
    app = choosing()
    async with app.run_test() as pilot:
        await pilot.press("j", "enter")
        assert app.results == [1]


async def test_a_choice_screen_preselects_the_first_choice() -> None:
    app = choosing()
    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert app.results == [0]


async def test_escape_on_a_choice_screen_dismisses_with_its_escape_index() -> None:
    app = choosing()
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert app.results == [2]


async def test_a_choice_screen_shows_its_title_and_message() -> None:
    app = choosing()
    async with app.run_test():
        box = app.choice_screen.query_one(Vertical)
        assert box.border_title == "Pick"
        assert str(app.choice_screen.query_one("#choice-message", Static).render()) == "Which?"


async def test_a_choice_screen_is_bordered_in_accent_or_as_an_error_with_the_class() -> None:
    for classes, colour in (((), "accent"), (("error",), "error")):
        app = choosing(*classes)
        async with app.run_test():
            border = app.choice_screen.query_one(Vertical).styles.border_top[1]
            assert border.hex == Color.parse(app.get_css_variables()[colour]).hex
