"""The window's list pane, which redraws a relabelled row without re-measuring the rest by
reaching into OptionList's private caches."""

from collections.abc import Sequence
from typing import ClassVar, cast

from textual import events
from textual.binding import Binding, BindingType
from textual.css.styles import RulesMap
from textual.visual import VisualType
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from orbit.tui.widgets import ModalFocus, relist

Row = tuple[str, VisualType]
"""A list row: the key it's tracked by and its label."""


class ListPane(ModalFocus, OptionList):
    """A window's list that only takes focus once activated (``enter_list_mode``), and
    ignores clicks on its rows until then; then j/k move and escape returns to window
    navigation."""

    _shown_rows: tuple[Row, ...] = ()
    _shown_keys: tuple[str | None, ...] = ()  # The rows' keys, then None for a trailing row.
    _measured_width: int | None = None  # The width the rows were last measured at.

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("escape", "normal_mode", show=False),
    ]

    def enter_list_mode(self) -> None:
        if self._activate() and self.highlighted is None and self.option_count:
            self.highlighted = 0

    # OptionList's handler takes no event, but this one needs it to stop that one running.
    def _on_resize(self, event: events.Resize) -> None:  # type: ignore[override]
        # OptionList forgets every row's height on any resize, and measuring a row renders it
        # again, which takes long for a long transcript; only a new width changes a height.
        width = self.scrollable_content_region.width
        if width == self._measured_width:
            event.prevent_default()  # Skips OptionList's handler, which runs after this one.
        self._measured_width = width

    def on_click(self, event: events.Click) -> None:
        # Clicks don't pick rows unless the list is active.
        if not self.has_focus:
            event.prevent_default()

    def row_lines(self) -> list[tuple[int, int]]:
        """Each row's first line and height, once the list is measured; none before."""
        tops, heights = self._index_to_line, self._heights
        return [(tops[i], heights[i]) for i in range(len(tops))]

    def show_rows(self, rows: Sequence[Row], trailing: Option | None = None) -> None:
        """Show ``rows``, then ``trailing`` if given. The same keys in the same order have the
        rows whose label changed relabelled in place; otherwise the rows are rebuilt, the
        highlight staying on its key's row, or on the nearest of ``rows`` when that row has
        gone, never on ``trailing``. A highlighted trailing row that's gone, or a highlighted
        row gone with no rows left, leaves nothing highlighted, so the highlight never lands
        on a row the user didn't choose."""
        rows = tuple(rows)
        keys = tuple(key for key, _ in rows) + (() if trailing is None else (None,))
        if keys == self._shown_keys:
            self._relabel_rows(rows)
            return
        shown, self._shown_rows, self._shown_keys = self._shown_keys, rows, keys
        labels = [label for _, label in rows] + ([] if trailing is None else [trailing])
        relist(self, labels, shown, keys)

    def _relabel_rows(self, rows: tuple[Row, ...]) -> None:
        """Relabel the shown rows whose label differs in ``rows``, which have the same keys."""
        before, self._shown_rows = self._shown_rows, rows
        for index, ((_, label), (_, old)) in enumerate(zip(rows, before, strict=True)):
            if label is not old and label != old:  # Unchanged labels are usually reused.
                self.replace_prompt(index, label)

    def replace_prompt(self, index: int, prompt: VisualType) -> None:
        """Replace the prompt of the option at ``index``, redrawing only that option's lines;
        if its height changed, it and the options after it are re-measured and the whole pane
        redrawn. A pane with no size, such as a hidden one, forgets all its rows' measurements
        instead, so they're measured afresh once it has a size, as Textual does on a resize."""
        # OptionList.replace_option_prompt_at_index clears every option's render and line
        # caches, so the next draw wraps the whole list again. Textual has no public way to
        # invalidate one option, so this reaches into its private caches; the tests in
        # tests/test_list_pane.py guard them.
        option = self.get_option_at_index(index)
        self._forget_render(option)
        option._set_prompt(prompt)
        line_cache = self._line_cache
        if not self.scrollable_content_region:  # Hidden: measure nothing, as OptionList does.
            line_cache.clear()
        elif index in line_cache.heights and not self._remeasure_if_resized(index):
            # Same height: only its own lines change, so only they are repainted.
            self.refresh_lines(line_cache.index_to_line[index], line_cache.heights[index])
            return
        self.refresh()  # Unmeasured, hidden or resized: later rows may move too.

    def _forget_render(self, option: Option) -> None:
        """Drop ``option``'s cached renders; part of ``replace_prompt``, on the same private
        caches."""
        render_cache = self._option_render_cache
        cached = render_cache.keys()  # LRUCache isn't iterable itself.
        for key in [key for key in cached if key[0] is option]:
            render_cache.discard(key)

    def _remeasure_if_resized(self, index: int) -> bool:
        """Whether the option at ``index`` changed height, re-measuring it and those after it
        if it did; part of ``replace_prompt``, on the same private caches."""
        option = self.get_option_at_index(index)
        width = self.scrollable_content_region.width - self._get_left_gutter_width()
        width -= self.get_component_styles("option-list--option").padding.width
        rules = cast(RulesMap, self.styles)  # As OptionList measures its options.
        height = self._get_visual(option).get_height(rules, width) + option._divider
        if height == self._line_cache.heights[index]:
            return False
        self._remeasure_from(index)
        return True

    def _remeasure_from(self, index: int) -> None:
        """Forget the lines of the options from ``index`` on and measure them again, keeping
        those before it; part of ``replace_prompt``, on the same private caches."""
        line_cache = self._line_cache
        del line_cache.lines[line_cache.index_to_line[index] :]
        for later in range(index, self.option_count):
            line_cache.heights.pop(later, None)
            line_cache.index_to_line.pop(later, None)
        self._update_lines()
