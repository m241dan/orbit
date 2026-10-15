from textual.app import App, ComposeResult

from orbit.tui.widgets import ListPane


class ListApp(App[None]):
    def __init__(self, *options: str) -> None:
        super().__init__()
        self.options = options

    def compose(self) -> ComposeResult:
        pane = ListPane(id="pane")
        pane.add_options(self.options)
        yield pane


async def test_list_mode_on_an_empty_pane_focuses_without_highlighting() -> None:
    app = ListApp()
    async with app.run_test() as pilot:
        pane = app.query_one(ListPane)
        assert not pane.can_focus
        pane.enter_list_mode()
        await pilot.pause()
        assert pane.has_focus
        assert pane.highlighted is None


async def test_list_mode_highlights_the_first_row_and_j_k_move() -> None:
    app = ListApp("one", "two")
    async with app.run_test() as pilot:
        pane = app.query_one(ListPane)
        pane.enter_list_mode()
        await pilot.pause()
        assert pane.highlighted == 0
        await pilot.press("j")
        assert pane.highlighted == 1
        await pilot.press("k")
        assert pane.highlighted == 0


async def test_escape_or_blur_makes_the_pane_unfocusable() -> None:
    app = ListApp("one")
    async with app.run_test() as pilot:
        pane = app.query_one(ListPane)
        pane.enter_list_mode()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not pane.has_focus
        assert not pane.can_focus
        pane.enter_list_mode()
        await pilot.pause()
        pane.blur()
        await pilot.pause()
        assert not pane.can_focus
