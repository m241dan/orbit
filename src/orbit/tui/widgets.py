"""Building blocks of the orbit screen."""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar, cast

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.css.styles import RulesMap
from textual.screen import ModalScreen
from textual.visual import VisualType
from textual.widget import Widget
from textual.widgets import Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from orbit.protocol import Settings
from orbit.tui.view import settings_summary

if TYPE_CHECKING:
    _WidgetMixin = Widget
else:
    # Not a Widget at runtime, so Textual still inherits the base's CSS and component classes.
    _WidgetMixin = object


class ModalFocus(_WidgetMixin):
    """Vim-style focus, mixed into a widget before its Textual base: the widget only takes
    focus once activated, and escape (bound by the widget to ``normal_mode``) or losing
    focus makes it unfocusable again, returning keys to the app."""

    can_focus = False

    def _activate(self) -> bool:
        """Whether the widget took focus; a disabled one doesn't."""
        if self.disabled:
            return False
        self.can_focus = True
        self.focus()
        return True

    def action_normal_mode(self) -> None:
        self.screen.set_focus(None)

    def on_blur(self) -> None:
        self.can_focus = False


Row = tuple[str, VisualType]
"""A list row: the key it's tracked by and its label."""


class ListPane(ModalFocus, OptionList):
    """A window's list that only takes focus once activated (``enter_list_mode``);
    then j/k move and escape returns to window navigation."""

    _shown_rows: tuple[Row, ...] = ()
    _shown_keys: tuple[str | None, ...] = ()  # The rows' keys, then None for a trailing row.

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("escape", "normal_mode", show=False),
    ]

    def enter_list_mode(self) -> None:
        if self._activate() and self.highlighted is None and self.option_count:
            self.highlighted = 0

    def show_rows(self, rows: Sequence[Row], trailing: Option | None = None) -> None:
        """Show ``rows``, then ``trailing`` if given. The same keys in the same order have the
        rows whose label changed relabelled in place; otherwise the rows are rebuilt, the
        highlight staying on its key's row, or on the nearest one when that row has gone. A
        highlighted trailing row that's gone leaves nothing highlighted, so the highlight
        never lands on a row the user didn't choose."""
        rows = tuple(rows)
        keys = tuple(key for key, _ in rows) + (() if trailing is None else (None,))
        if keys == self._shown_keys:
            self._relabel_rows(rows)
            return
        highlight = self._kept_highlight(keys)
        self._shown_rows, self._shown_keys = rows, keys
        self.clear_options()
        self.add_options([label for _, label in rows] + ([] if trailing is None else [trailing]))
        self.highlighted = highlight

    def _relabel_rows(self, rows: tuple[Row, ...]) -> None:
        """Relabel the shown rows whose label differs in ``rows``, which have the same keys."""
        before, self._shown_rows = self._shown_rows, rows
        for index, ((_, label), (_, old)) in enumerate(zip(rows, before, strict=True)):
            if label != old:
                self.replace_prompt(index, label)

    def _kept_highlight(self, keys: tuple[str | None, ...]) -> int | None:
        """Where the highlight goes once rows with ``keys`` are shown: on the row with its
        key, or the nearest one when that's gone, or nowhere when the trailing row has gone."""
        index = self.highlighted
        if index is None:
            return None
        key = self._shown_keys[index]
        if key in keys:
            return keys.index(key)
        return None if key is None else min(index, len(keys) - 1)

    def replace_prompt(self, index: int, prompt: VisualType) -> None:
        """Replace the prompt of the option at ``index``, redrawing only that option and, if
        its height changed, re-measuring only it and the options after it."""
        # OptionList.replace_option_prompt_at_index clears every option's render and line
        # caches, so the next draw wraps the whole list again. Textual has no public way to
        # invalidate one option, so this reaches into its private caches; the tests in
        # tests/test_list_pane.py guard them.
        option = self.get_option_at_index(index)
        render_cache = self._option_render_cache
        cached = render_cache.keys()  # LRUCache isn't iterable itself.
        for key in [key for key in cached if key[0] is option]:
            render_cache.discard(key)
        option._set_prompt(prompt)
        line_cache = self._line_cache
        height = line_cache.heights.get(index)
        if height is None:  # Not measured yet, so measured as the list is laid out.
            self.refresh()
            return
        width = self.scrollable_content_region.width - self._get_left_gutter_width()
        width -= self.get_component_styles("option-list--option").padding.width
        rules = cast(RulesMap, self.styles)  # As OptionList measures its options.
        if self._get_visual(option).get_height(rules, width) + option._divider != height:
            self._remeasure_from(index)
        self.refresh()

    def _remeasure_from(self, index: int) -> None:
        """Forget the lines of the options from ``index`` on and measure them again, keeping
        those before it; part of ``replace_prompt``, on the same private caches."""
        line_cache = self._line_cache
        del line_cache.lines[line_cache.index_to_line[index] :]
        for later in range(index, self.option_count):
            line_cache.heights.pop(later, None)
            line_cache.index_to_line.pop(later, None)
        self._update_lines()


class SidePanel(VerticalScroll):
    """Titled pane in the right-hand column."""

    can_focus = False

    DEFAULT_CSS = """
    SidePanel {
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    """

    def __init__(self, title: str, *, id: str) -> None:
        super().__init__(id=id)
        self.border_title = title


class PromptInput(ModalFocus, Input):
    """Single-line prompt box at the bottom of the screen.

    Vim-style: it only takes focus in insert mode (``enter_insert_mode``), and
    escape or losing focus returns to normal mode, where keys go to the app.
    """

    DEFAULT_CSS = """
    PromptInput {
        height: 3;
        border: round $secondary;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "normal_mode", "Normal mode", show=False)
    ]

    def __init__(self, **kwargs: Any) -> None:
        # Keep the draft unselected so re-entering insert mode appends to it.
        super().__init__(select_on_focus=False, **kwargs)

    def on_mount(self) -> None:
        self.border_title = "Prompt · planner"

    def enter_insert_mode(self) -> None:
        self._activate()

    def show_settings(self, settings: Settings) -> None:
        self.border_subtitle = settings_summary(settings)


class PromptBox(ModalFocus, TextArea):
    """Multi-line prompt box, for a task's whole prompt.

    Vim-style like ``PromptInput``: it only takes focus in insert mode
    (``enter_insert_mode``), which puts the cursor at the end so typing appends, and
    escape or losing focus returns to normal mode, where keys go to the app.
    """

    DEFAULT_CSS = """
    PromptBox {
        height: 12;
        border: round $secondary;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "normal_mode", "Normal mode", show=False)
    ]

    def __init__(self, text: str = "", **kwargs: Any) -> None:
        super().__init__(text, soft_wrap=True, show_line_numbers=False, **kwargs)

    def enter_insert_mode(self) -> None:
        if self._activate():
            self.move_cursor(self.document.end)


class PickerScreen(ModalScreen[int | None]):
    """Pop-up list of options. Dismisses with the chosen index, or None on escape.

    Meant to be built once and installed so reopening it is instant.
    """

    DEFAULT_CSS = """
    PickerScreen { align: center middle; }
    PickerScreen > OptionList {
        width: 70;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        padding: 0 1;
    }
    PickerScreen > OptionList, PickerScreen > OptionList:focus { border: round $accent; }
    """
    AUTO_FOCUS = "OptionList"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close"),
        # Vim-style movement alongside the list's own arrow keys.
        Binding("j", "move('down')", "Down", show=False),
        Binding("k", "move('up')", "Up", show=False),
    ]

    def __init__(self, title_text: str, labels: Sequence[str]) -> None:
        super().__init__()
        self.title_text = title_text
        self._labels = list(labels)
        # Index to highlight each time the picker is shown.
        self.current: int | None = None

    def compose(self) -> ComposeResult:
        options = OptionList(*self._labels)
        options.border_title = self.title_text
        yield options

    def set_labels(self, labels: Sequence[str]) -> None:
        self._labels = list(labels)
        if self.is_mounted:
            options = self.query_one(OptionList)
            options.clear_options()
            options.add_options(self._labels)

    def on_screen_resume(self) -> None:
        self.query_one(OptionList).highlighted = self.current

    @on(OptionList.OptionSelected)
    def _select(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_index)

    def action_move(self, direction: str) -> None:
        options = self.query_one(OptionList)
        if direction == "down":
            options.action_cursor_down()
        else:
            options.action_cursor_up()

    def action_close(self) -> None:
        self.dismiss(None)


CONFIGURE_TITLE = "Set up checks?"
CONFIGURE_MESSAGE = "A configurator task will go to the front of the task list and run now."
CONFIGURE_YES = "set up"


class ConfirmScreen(ModalScreen[bool]):
    """Asks ``title`` with ``message``; dismisses with True on ``y``, False on ``n`` or escape.

    ``yes`` names what ``y`` does in the keys shown. Enter doesn't answer, so a stray
    keypress can't confirm.
    """

    AUTO_FOCUS = None
    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    ConfirmScreen > Static {
        width: 60;
        max-width: 90%;
        height: auto;
        border: round $accent;
        padding: 0 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "answer(True)", show=False),
        Binding("n", "answer(False)", show=False),
        Binding("escape", "answer(False)", show=False),
    ]

    def __init__(self, title: str, message: str, yes: str = "confirm") -> None:
        super().__init__()
        self.title_text = title
        self.message = message
        self.yes = yes

    def compose(self) -> ComposeResult:
        body = Static(Text(self.message), id="confirm-message")
        body.border_title = self.title_text
        body.border_subtitle = f"y {self.yes} · n/esc cancel"
        yield body

    def action_answer(self, confirmed: bool) -> None:
        self.dismiss(confirmed)
