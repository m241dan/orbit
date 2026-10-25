from typing import Self

import pytest
from textual.app import App, ComposeResult
from textual.geometry import Region
from textual.visual import VisualType
from textual.widgets.option_list import Option

from orbit.tui.list_pane import ListPane


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


def prompts(pane: ListPane) -> list[str]:
    return [str(pane.get_option_at_index(i).prompt) for i in range(pane.option_count)]


async def test_shown_rows_end_with_the_trailing_row() -> None:
    app = ListApp()
    async with app.run_test() as pilot:
        pane = app.query_one(ListPane)
        pane.show_rows([("a", "one"), ("b", "two")], Option("more"))
        await pilot.pause()
        assert prompts(pane) == ["one", "two", "more"]
        pane.show_rows([])
        await pilot.pause()
        assert prompts(pane) == []


async def test_rows_with_the_same_keys_relabel_only_the_changed_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = ListApp()
    async with app.run_test() as pilot:
        pane = app.query_one(ListPane)
        pane.show_rows([("a", "one"), ("b", "two")])
        await pilot.pause()
        kept = pane.get_option_at_index(0)
        relabelled: list[int] = []
        replace = pane.replace_prompt

        def spy(index: int, prompt: VisualType) -> None:
            relabelled.append(index)
            replace(index, prompt)

        monkeypatch.setattr(pane, "replace_prompt", spy)
        pane.show_rows([("a", "one"), ("b", "deux")])
        await pilot.pause()
        assert prompts(pane) == ["one", "deux"]
        assert relabelled == [1]
        assert pane.get_option_at_index(0) is kept


@pytest.mark.parametrize(
    ("keys", "highlight"),
    [
        (["b", "c"], 0),  # A row above it went: the highlight follows its key up.
        (["x", "a", "b", "c"], 2),  # One appeared above it: it follows its key down.
        (["a", "c"], 1),  # Its own row went: the nearest row in range.
        (["a"], 0),
    ],
)
async def test_the_highlight_follows_its_key_when_the_rows_change(
    keys: list[str], highlight: int
) -> None:
    app = ListApp()
    async with app.run_test() as pilot:
        pane = app.query_one(ListPane)
        pane.show_rows([("a", "a"), ("b", "b"), ("c", "c")])
        pane.highlighted = 1
        pane.show_rows([(key, key) for key in keys])
        await pilot.pause()
        assert pane.highlighted == highlight


async def test_the_highlight_stays_on_the_trailing_row() -> None:
    app = ListApp()
    async with app.run_test() as pilot:
        pane = app.query_one(ListPane)
        pane.show_rows([("a", "a")], Option("more"))
        pane.highlighted = 1
        pane.show_rows([("b", "b"), ("a", "a")], Option("more"))
        await pilot.pause()
        assert pane.highlighted == 2


async def test_rows_shown_without_a_highlight_highlight_nothing() -> None:
    app = ListApp()
    async with app.run_test() as pilot:
        pane = app.query_one(ListPane)
        pane.show_rows([("a", "a")])
        pane.show_rows([("b", "b"), ("a", "a")])
        await pilot.pause()
        assert pane.highlighted is None


async def test_a_prompt_replaced_while_hidden_is_measured_once_shown() -> None:
    app = ListApp("one", "two", "three")
    async with app.run_test() as pilot:
        pane = app.query_one(ListPane)
        assert line_map(pane) == [0, 1, 2]
        pane.display = False
        await pilot.pause()
        pane.replace_prompt(1, "a\nb\nc")
        pane.display = True
        await pilot.pause()
        assert line_map(pane) == [0, 1, 1, 1, 2]
        assert [pane.render_line(y).text.strip() for y in range(1, 4)] == ["a", "b", "c"]


class RefreshSpy(ListPane):
    """A pane recording the regions each refresh asked for; empty for the whole pane."""

    def __init__(self, *, id: str) -> None:
        self.refreshed: list[tuple[Region, ...]] = []
        super().__init__(id=id)

    def refresh(self, *regions: Region, **kwargs: bool) -> Self:
        self.refreshed.append(regions)
        return super().refresh(*regions, **kwargs)


class SpyApp(ListApp):
    def compose(self) -> ComposeResult:
        pane = RefreshSpy(id="pane")
        pane.add_options(self.options)
        yield pane


async def test_a_prompt_of_the_same_height_repaints_only_its_lines() -> None:
    app = SpyApp("one", "a\nb", "three")
    async with app.run_test() as pilot:
        pane = app.query_one(RefreshSpy)
        await pilot.pause()  # Measured.
        pane.refreshed = []
        pane.replace_prompt(1, "c\nd")
        assert pane.refreshed == [(Region(0, 1, pane.size.width, 2),)]
        await pilot.pause()
        assert pane.render_line(2).text.strip() == "d"


async def test_a_prompt_of_another_height_repaints_the_whole_pane() -> None:
    app = SpyApp("one", "two", "three")
    async with app.run_test():
        pane = app.query_one(RefreshSpy)
        pane.refreshed = []
        pane.replace_prompt(1, "a\nb")
        assert () in pane.refreshed


class ClearSpy(ListPane):
    """A pane counting how often OptionList forgets its rows' measurements."""

    clears = 0

    def _clear_caches(self) -> None:
        self.clears += 1
        super()._clear_caches()


class ClearApp(ListApp):
    CSS = "ClearSpy { height: 1fr; }"

    def compose(self) -> ComposeResult:
        pane = ClearSpy(id="pane")
        pane.add_options(self.options)
        yield pane


async def test_only_a_new_width_makes_the_pane_measure_its_rows_again() -> None:
    app = ClearApp("one", "two", "three")
    async with app.run_test(size=(80, 24)) as pilot:
        pane = app.query_one(ClearSpy)
        await pilot.pause()
        pane.clears = 0
        await pilot.resize_terminal(80, 30)
        await pilot.pause()
        assert pane.clears == 0
        assert pane.render_line(2).text.strip() == "three"
        await pilot.resize_terminal(60, 30)
        await pilot.pause()
        assert pane.clears == 1
