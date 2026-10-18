from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.widget import Widget
from textual.widgets._footer import FooterKey

from orbit.tui.widgets import KeyFooter


class Plain(Widget, can_focus=True):
    pass


class Saving(Widget, can_focus=True):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("ctrl+s", "save", "Save")]


class FooterApp(App[None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("ctrl+q", "quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Plain(id="first")
        yield Plain(id="second")
        yield Saving(id="saving")
        yield KeyFooter()


def keys(app: FooterApp) -> list[FooterKey]:
    return list(app.query(FooterKey))


async def test_the_footer_shows_the_active_keys() -> None:
    app = FooterApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert [key.description for key in keys(app)] == ["Quit", "palette"]


async def test_the_footer_keeps_its_keys_when_focus_brings_the_same_ones() -> None:
    app = FooterApp()
    async with app.run_test() as pilot:
        app.query_one("#first").focus()
        await pilot.pause()
        before = keys(app)

        app.query_one("#second").focus()
        await pilot.pause()

        assert keys(app) == before


async def test_the_footer_redraws_when_focus_brings_other_keys() -> None:
    app = FooterApp()
    async with app.run_test() as pilot:
        app.query_one("#saving").focus()
        await pilot.pause()

        assert [key.description for key in keys(app)] == ["Save", "Quit", "palette"]
