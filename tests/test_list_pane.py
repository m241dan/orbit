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


def line_map(pane: ListPane) -> list[int]:
    """Which option each of the pane's lines belongs to, from the meta its clicks carry."""
    return [
        next(iter(pane.render_line(y))).style.meta["option"]  # type: ignore[union-attr]
        for y in range(pane.virtual_size.height)
    ]


async def test_a_replaced_prompt_is_drawn() -> None:
    app = ListApp("one", "two", "three")
    async with app.run_test() as pilot:
        pane = app.query_one(ListPane)
        assert pane.render_line(1).text.strip() == "two"
        pane.replace_prompt(1, "deux")
        await pilot.pause()
        assert pane.get_option_at_index(1).prompt == "deux"
        assert pane.render_line(1).text.strip() == "deux"


async def test_a_taller_then_shorter_prompt_moves_the_rows_after_it() -> None:
    app = ListApp("one", "two", "three")
    async with app.run_test() as pilot:
        pane = app.query_one(ListPane)
        assert line_map(pane) == [0, 1, 2]
        pane.replace_prompt(1, "a\nb\nc")
        await pilot.pause()
        assert pane.virtual_size.height == 5
        assert line_map(pane) == [0, 1, 1, 1, 2]
        assert [pane.render_line(y).text.strip() for y in range(5)] == [
            "one",
            "a",
            "b",
            "c",
            "three",
        ]
        pane.replace_prompt(0, "x\ny")
        pane.replace_prompt(1, "b")
        await pilot.pause()
        assert line_map(pane) == [0, 0, 1, 2]
        assert [pane.render_line(y).text.strip() for y in range(4)] == ["x", "y", "b", "three"]


async def test_a_prompt_of_the_same_height_leaves_the_line_map_alone() -> None:
    app = ListApp("one", "two", "three")
    async with app.run_test() as pilot:
        pane = app.query_one(ListPane)
        pane.replace_prompt(2, "a\nb")
        await pilot.pause()
        pane.replace_prompt(2, "c\nd")
        await pilot.pause()
        assert line_map(pane) == [0, 1, 2, 2]
        assert pane.render_line(3).text.strip() == "d"


async def test_replacing_keeps_the_highlight_and_clicks_pick_the_clicked_row() -> None:
    app = ListApp("one", "two", "three")
    async with app.run_test() as pilot:
        pane = app.query_one(ListPane)
        pane.enter_list_mode()
        await pilot.press("j")
        pane.replace_prompt(0, "a\nb\nc")
        pane.replace_prompt(1, "x\ny")
        await pilot.pause()
        assert pane.highlighted == 1
        for y, index in ((0, 0), (2, 0), (3, 1), (4, 1), (5, 2)):
            await pilot.click(pane, offset=(2, pane.styles.gutter.top + y))
            assert pane.highlighted == index


async def test_a_prompt_replaced_before_the_pane_is_laid_out_is_measured_when_it_is() -> None:
    app = ListApp()
    async with app.run_test() as pilot:
        pane = ListPane()
        pane.add_options(["one", "two"])
        pane.replace_prompt(0, "a\nb")
        await app.mount(pane)
        await pilot.pause()
        assert line_map(pane) == [0, 0, 1]
