"""The shared Vim-style movement of the list modals, and the status lines by the prompt."""

from typing import Any

from textual.app import App, ComposeResult
from textual.color import Color
from textual.containers import Vertical
from textual.widgets import Input, OptionList, Static

from orbit.protocol import (
    AddWorkspace,
    DirectoryEntry,
    DirectoryListing,
    ListDirectory,
    SessionSummary,
)
from orbit.tui.explorer import ExplorerScreen
from orbit.tui.sessions import SessionChange, SessionsScreen
from orbit.tui.view.common import kept_highlight
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.sessions import Viewing
from orbit.tui.widgets import (
    ChoiceScreen,
    ListScreen,
    NoteLine,
    PickerScreen,
    StatusLine,
    relist,
)

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


class RefusingOne:
    """Sends commands to nowhere, refusing the one numbered ``refused``, from 1, as ``no`` and
    leaving the rest unanswered."""

    def __init__(self, refused: int) -> None:
        self.refused = refused
        self.sent: list[object] = []

    async def __call__(
        self, command: SessionChange | ListDirectory | AddWorkspace, awaited: Awaited
    ) -> None:
        self.sent.append(command)
        if len(self.sent) == self.refused and awaited.why is not None:
            awaited.why("no")


def note_colour(screen: ListScreen[Any]) -> str:
    """The hex colour of the note under ``screen``'s list."""
    return screen.query_one("#picker-note", StatusLine).styles.color.hex


async def test_a_refused_rename_shows_under_the_list_as_an_error_until_the_next() -> None:
    manage = RefusingOne(1)
    screen = SessionsScreen(SESSIONS, Viewing(workspace="w", attached=None), manage)
    app: App[None] = App()
    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()
        await pilot.press("r", "x", "enter")
        assert screen.note == "no"
        assert note_colour(screen) == Color.parse(app.get_css_variables()["error"]).hex
        await pilot.press("r", "y", "enter")
        assert screen.note is None and len(manage.sent) == 2


async def test_a_refused_listing_shows_under_the_explorers_list_as_an_error_until_the_next() -> (
    None
):
    browse = RefusingOne(2)
    screen = ExplorerScreen(browse)
    app: App[None] = App()
    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()
        docs = DirectoryEntry(name="docs", is_repository=False, hidden=False)
        screen.show_listing(DirectoryListing(path="/srv", parent="/", entries=(docs,), total=1))
        await pilot.press("l")
        assert screen.note == "no"
        assert note_colour(screen) == Color.parse(app.get_css_variables()["error"]).hex
        await pilot.press("h")
        assert screen.note is None and browse.sent[-1] == ListDirectory(path="/")


class Listing(App[None]):
    def compose(self) -> ComposeResult:
        yield OptionList("a", "b", "c")


async def test_relist_keeps_the_highlight_on_its_row_while_that_is_still_listed() -> None:
    app = Listing()
    async with app.run_test():
        options = app.query_one(OptionList)
        options.highlighted = 1
        relist(options, ["new", "a", "b"], ["a", "b", "c"], ["new", "a", "b"])
        assert options.highlighted == 2
        assert str(options.get_option_at_index(0).prompt) == "new"


async def test_relist_falls_back_where_kept_highlight_says_once_its_row_has_gone() -> None:
    old, new = ["a", "b", "c"], ["a", "b", None]
    app = Listing()
    async with app.run_test():
        options = app.query_one(OptionList)
        options.highlighted = 2
        relist(options, ["a", "b", "add"], old, new)
        assert options.highlighted == kept_highlight(old, 2, new) == 1
        assert options.option_count == 3


async def test_relist_with_nothing_left_highlights_nothing() -> None:
    app = Listing()
    async with app.run_test():
        options = app.query_one(OptionList)
        options.highlighted = 1
        relist(options, [], ["a", "b", "c"], [])
        assert options.highlighted is None
        assert options.option_count == 0


class Notes(App[None]):
    def compose(self) -> ComposeResult:
        yield NoteLine("", id="note")


async def test_a_note_line_is_hidden_until_it_shows_a_note() -> None:
    app = Notes()
    async with app.run_test():
        assert app.query_one("#note", NoteLine).shown is None


async def test_a_note_line_shows_its_note_and_none_hides_it() -> None:
    app = Notes()
    async with app.run_test():
        line = app.query_one("#note", NoteLine)
        line.show("12 more")
        assert line.shown == "12 more"
        line.show(None)
        assert line.shown is None


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
