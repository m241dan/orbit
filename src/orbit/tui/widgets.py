"""Building blocks of the orbit screen."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, cast

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.css.styles import RulesMap
from textual.message import Message
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.visual import VisualType
from textual.widget import AwaitMount, Widget
from textual.widgets import Footer, Input, OptionList, Static, TextArea
from textual.widgets._footer import FooterKey
from textual.widgets.option_list import Option

from orbit.protocol import Settings
from orbit.tui.view import SPINNER_FPS, confirm_hint, prompt_title, settings_summary

if TYPE_CHECKING:
    _WidgetMixin = Widget
else:
    # Not a Widget at runtime, so Textual still inherits the base's CSS and component classes.
    _WidgetMixin = object


class Ticker:
    """Calls ``tick`` as fast as spinners turn, on ``owner``'s timer, but only while kept
    running, so nothing redraws while nothing spins."""

    def __init__(self, owner: Widget, tick: Callable[[], None]) -> None:
        self._owner = owner
        self._tick = tick
        self._timer: Timer | None = None

    @property
    def ticking(self) -> bool:
        """Whether the timer runs."""
        return self._timer is not None

    def keep(self, running: bool) -> None:
        """Tick while ``running``, starting the timer if it's stopped, and stop it if not."""
        if running and self._timer is None:
            self._timer = self._owner.set_interval(1 / SPINNER_FPS, self._tick)
        elif not running and self._timer is not None:
            self._timer.stop()
            self._timer = None


def mark_selected(widget: Widget, selected: bool) -> None:
    """Mark ``widget`` selected with the ``selected`` class, or unmark it, restyling only it."""
    # The class styles the widget's own border, but a class change restyles all the widget
    # holds, such as each of the conversation's logs and their component classes.
    widget.set_class(selected, "selected", update=False)
    widget.app.stylesheet.apply(widget)


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

    def row_lines(self) -> list[tuple[int, int]]:
        """Each row's first line and height, once the list is measured; none before."""
        tops, heights = self._index_to_line, self._heights
        return [(tops[i], heights[i]) for i in range(len(tops))]

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


FooterEntry = tuple[Binding, bool, str]
"""A key the footer shows: its binding, whether it's enabled and its tooltip."""


class KeyFooter(Footer):
    """Footer that changes only the keys whose binding or state changes: Textual's rebuilds
    them all on every focus change, remounting and restyling each key. Keys that come, go
    or change state are removed or mounted one by one; grouped keys and the command
    palette's key are left to Textual's footer, which rebuilds them all."""

    # The keys last shown, with the footer's style; None before the first.
    _shown: tuple[tuple[FooterEntry, ...], bool, bool] | None = None
    # Each key's widget, in order, while this footer makes them; empty while Textual's does.
    _keys: Mapping[FooterEntry, FooterKey] = MappingProxyType({})

    async def recompose(self) -> None:
        if not self.is_attached:  # As Textual's recompose, which a closing app may call.
            return
        entries = self._entries()
        shown = (entries, self.compact, self.show_command_palette)
        if shown == self._shown:
            return
        self._shown = shown
        if self._patchable(entries):
            await self._patch(entries)
            return
        self._keys = {}
        await super().recompose()

    def _entries(self) -> tuple[FooterEntry, ...]:
        """The keys to show, the first binding of each action as Textual's footer picks
        them, then the command palette's when it shows."""
        active = self.screen.active_bindings
        firsts: dict[str, FooterEntry] = {}
        for _, binding, enabled, tooltip in active.values():
            if binding.show:
                firsts.setdefault(binding.action, (binding, enabled, tooltip))
        palette = active.get(self.app.COMMAND_PALETTE_BINDING)
        if palette is None or not self._shows_palette():
            return tuple(firsts.values())
        return (*firsts.values(), palette[1:])

    def _shows_palette(self) -> bool:
        """Whether the footer shows the command palette's key, if it's bound."""
        return self.show_command_palette and self.app.ENABLE_COMMAND_PALETTE

    def _patchable(self, entries: tuple[FooterEntry, ...]) -> bool:
        """Whether ``entries`` are shown one key widget each, in order: none grouped and no
        command palette's key."""
        return not self._shows_palette() and all(binding.group is None for binding, *_ in entries)

    def _keeps_order(self, entries: tuple[FooterEntry, ...]) -> bool:
        """Whether the keys shown that stay in ``entries`` keep their order."""
        kept = [entry for entry in self._keys if entry in entries]
        return kept == [entry for entry in entries if entry in self._keys]

    async def _patch(self, entries: tuple[FooterEntry, ...]) -> None:
        """Show ``entries``, a key widget each: remove the keys gone and mount the new ones in
        their places, or all of them anew when those staying change order or Textual's footer
        made them."""
        old = self._keys if self._keeps_order(entries) else {}
        self._keys = self._keys_for(entries, old)
        kept = set(self._keys.values())
        # Removed and mounted before awaiting either, so nothing changes in between.
        removed = self.remove_children([child for child in self.children if child not in kept])
        mounts = self._mount_new(old)
        await removed
        for mounted in mounts:
            await mounted

    def _keys_for(
        self, entries: tuple[FooterEntry, ...], old: Mapping[FooterEntry, FooterKey]
    ) -> dict[FooterEntry, FooterKey]:
        """A key widget for each of ``entries``: its widget in ``old``, or a new one."""
        return {entry: old[entry] if entry in old else self._footer_key(entry) for entry in entries}

    def _mount_new(self, old: Mapping[FooterEntry, FooterKey]) -> list[AwaitMount]:
        """Mount the keys not in ``old``, each before the key after it."""
        mounts: list[AwaitMount] = []
        before: FooterKey | None = None
        for entry, key in reversed(list(self._keys.items())):
            if entry not in old:
                mounts.append(self.mount(key, before=before))
            before = key
        return mounts

    def _footer_key(self, entry: FooterEntry) -> FooterKey:
        """A key widget for ``entry``, made as Textual's footer makes an ungrouped one."""
        binding, enabled, tooltip = entry
        key = FooterKey(
            binding.key,
            self.app.get_key_display(binding),
            binding.description,
            binding.action,
            disabled=not enabled,
            tooltip=tooltip,
        )
        # Born with the footer's style, so binding it doesn't restyle the key once mounted.
        key.set_reactive(FooterKey.compact, self.compact)
        return key.data_bind(compact=Footer.compact)


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
    """Single-line input, for a task's title.

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

    def enter_insert_mode(self) -> None:
        self._activate()


class ModalTextArea(ModalFocus, TextArea):
    """Text area with Vim-style focus: it only takes focus in insert mode
    (``enter_insert_mode``), which puts the cursor at the end so typing appends."""

    def enter_insert_mode(self) -> None:
        if self._activate():
            self.move_cursor(self.document.end)


class ChatPrompt(ModalTextArea):
    """Prompt box at the bottom of the screen, which wraps its text and grows with it from
    one row up to eight, then scrolls; enter sends it, and pasted newlines are kept.

    Vim-style like ``ModalTextArea``: escape or losing focus returns to normal mode.
    """

    DEFAULT_CSS = """
    ChatPrompt {
        height: auto;
        max-height: 10;
        border: round $secondary;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "normal_mode", "Normal mode", show=False),
        # Priority, to beat the text area's own enter, which types a newline.
        Binding("enter", "submit", show=False, priority=True),
    ]

    class Submitted(Message):
        """The user sent ``text``."""

        def __init__(self, prompt: "ChatPrompt", text: str) -> None:
            super().__init__()
            self.prompt = prompt
            self.text = text

        @property
        def control(self) -> "ChatPrompt":
            return self.prompt

    def __init__(self, *, placeholder: str = "", id: str | None = None) -> None:
        super().__init__(soft_wrap=True, show_line_numbers=False, placeholder=placeholder, id=id)

    def on_mount(self) -> None:
        self.show_busy(False)

    def show_busy(self, busy: bool) -> None:
        self.border_title = prompt_title(busy)

    def show_settings(self, settings: Settings) -> None:
        self.border_subtitle = settings_summary(settings)

    def action_submit(self) -> None:
        self.post_message(self.Submitted(self, self.text))


class PromptBox(ModalTextArea):
    """Multi-line prompt box, for a task's whole prompt.

    Vim-style like ``ModalTextArea``: escape or losing focus returns to normal mode, where
    keys go to the app.
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

    def choose[T](
        self,
        options: Sequence[T],
        current: int | None,
        chosen: Callable[[T], Awaitable[None] | None],
    ) -> None:
        """Show the picker with option ``current`` highlighted and call ``chosen`` with the
        item of ``options`` picked; escape calls nothing."""
        self.current = current

        async def picked(index: int | None) -> None:
            if index is not None:
                result = chosen(options[index])
                if result is not None:
                    await result

        self.app.push_screen(self, picked)

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
        body.border_subtitle = confirm_hint(self.yes)
        yield body

    def action_answer(self, confirmed: bool) -> None:
        self.dismiss(confirmed)
