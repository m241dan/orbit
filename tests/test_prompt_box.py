from textual.app import App, ComposeResult

from orbit.tui.widgets import PromptBox


class BoxApp(App[None]):
    def __init__(self, text: str = "", *, disabled: bool = False) -> None:
        super().__init__()
        self.text = text
        self.box_disabled = disabled

    def compose(self) -> ComposeResult:
        yield PromptBox(self.text, id="box", disabled=self.box_disabled)


async def test_starts_in_normal_mode_where_keys_and_clicks_leave_it_alone() -> None:
    app = BoxApp("draft")
    async with app.run_test() as pilot:
        box = app.query_one(PromptBox)
        assert not box.can_focus
        assert app.focused is None
        await pilot.press("a", "b")
        assert box.text == "draft"
        await pilot.click(PromptBox)
        await pilot.pause()
        assert app.focused is None
        assert box.text == "draft"


async def test_insert_mode_focuses_and_appends_typed_lines() -> None:
    app = BoxApp("first")
    async with app.run_test() as pilot:
        box = app.query_one(PromptBox)
        box.enter_insert_mode()
        await pilot.pause()
        assert box.has_focus
        await pilot.press("enter", "t", "w", "o")
        assert box.text == "first\ntwo"


async def test_escape_keeps_the_text_and_returns_to_normal_mode() -> None:
    app = BoxApp()
    async with app.run_test() as pilot:
        box = app.query_one(PromptBox)
        box.enter_insert_mode()
        await pilot.pause()
        await pilot.press("h", "i", "escape")
        await pilot.pause()
        assert app.focused is None
        assert box.text == "hi"
        assert not box.can_focus
        await pilot.press("x")
        assert box.text == "hi"


async def test_insert_mode_does_nothing_while_disabled() -> None:
    app = BoxApp(disabled=True)
    async with app.run_test() as pilot:
        box = app.query_one(PromptBox)
        box.enter_insert_mode()
        await pilot.pause()
        assert app.focused is None
        assert not box.can_focus
