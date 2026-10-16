"""The conversation window: the transcript and its per-tab logs."""

from dataclasses import dataclass
from typing import Any, ClassVar, get_args

from rich.console import Console, ConsoleOptions, RenderResult
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import OptionList

from orbit.tui.view import EntryKind, collapsible, entry_label, gutter, starts_collapsed
from orbit.tui.widgets import ListPane

Entry = tuple[str, EntryKind]
"""A transcript entry: its text and how it's styled."""


@dataclass(frozen=True)
class GutteredEntry:
    """A transcript entry as drawn: its ``label`` wrapped to the width left beside the
    ``gutter``, which goes in front of every row in ``gutter_style``, then a blank row so the
    entry reads as a paragraph."""

    label: Text
    gutter: str
    gutter_style: Style

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = options.max_width - len(self.gutter)
        for row in console.render_lines(self.label, options.update_width(width), pad=False):
            yield Segment(self.gutter, self.gutter_style)
            yield from row
            yield Segment.line()
        yield Segment.line()


class TranscriptLog(ListPane):
    """One tab's entries, each an option: activated, j/k move between them and enter collapses
    or expands one. Kept at no display until shown, since the list lays out only what's on
    screen, so switching tabs is instant and adding an entry costs the same however long the
    conversation gets. The cursor's entry is marked in the left gutter rather than highlighted."""

    DEFAULT_CSS = """
    TranscriptLog {
        display: none;
        height: 1fr;
        max-height: 100%;
        border: none;
        padding: 0;
        background: transparent;
        scrollbar-gutter: stable;
    }
    TranscriptLog:focus { border: none; background-tint: $foreground 0%; }
    TranscriptLog.-shown { display: block; }
    TranscriptLog .transcript--user { color: $text-accent; text-style: bold; }
    TranscriptLog .transcript--tool { color: $text-muted; }
    TranscriptLog .transcript--verify { color: $text-warning; }
    TranscriptLog .transcript--agent { color: $text-accent; }
    TranscriptLog .transcript--request { color: $text-accent; }
    TranscriptLog .transcript--error { color: $text-error; }
    TranscriptLog .transcript--gutter { color: $text-muted; }
    TranscriptLog .transcript--marker { color: $accent; }
    TranscriptLog > .option-list--option-highlighted,
    TranscriptLog:focus > .option-list--option-highlighted,
    TranscriptLog > .option-list--option-hover {
        color: $foreground;
        background: transparent;
        text-style: none;
    }
    """
    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "transcript--gutter",
        "transcript--marker",
        *(f"transcript--{kind}" for kind in get_args(EntryKind)),
    }

    def __init__(self, shown: bool = False) -> None:
        super().__init__()
        self._entries: list[Entry] = []
        self._collapsed: list[bool] = []
        self._marked: int | None = None  # The entry drawn with the marker.
        self.set_class(shown, "-shown")

    @property
    def entries(self) -> tuple[Entry, ...]:
        return tuple(self._entries)

    def enter_list_mode(self) -> None:
        """Activate the list, starting at the latest entry when it holds no cursor."""
        if self.highlighted is None and self.option_count:
            self.highlighted = self.option_count - 1
        super().enter_list_mode()

    def append(self, text: str, kind: EntryKind) -> None:
        """Add an entry, collapsed if its kind starts so; the view stays pinned to the latest
        entry unless the list is active or holds a cursor."""
        collapsed = starts_collapsed(kind) and collapsible(text)
        self._entries.append((text, kind))
        self._collapsed.append(collapsed)
        self.add_option(self._prompt(len(self._entries) - 1))
        if not self.has_focus and self.highlighted is None:
            self.scroll_end(animate=False)

    def release_cursor(self) -> None:
        """Let go of the held cursor, so the view follows the latest entry again."""
        self.highlighted = None
        self.scroll_end(animate=False)

    def show_place(self) -> None:
        """Scroll to the held cursor's entry, or to the latest entry if there's none."""
        if self.highlighted is None:
            self.scroll_end(animate=False)
        else:
            self.scroll_to_highlight()

    def collapsed(self, index: int) -> bool:
        return self._collapsed[index]

    def toggle(self, index: int) -> None:
        """Collapse the entry at ``index`` if it's expanded, and the other way round; an entry
        of one line stays as it is."""
        text, _ = self._entries[index]
        if not collapsible(text):
            return
        self._collapsed[index] = not self._collapsed[index]
        self._redraw(index)

    def watch_highlighted(self, highlighted: int | None) -> None:
        """Move the marker from the entry it was on to the cursor's, if there is one."""
        super().watch_highlighted(highlighted)
        previous, self._marked = self._marked, highlighted
        for index in (previous, highlighted):
            if index is not None:
                self._redraw(index)

    @on(OptionList.OptionSelected)
    def _toggle(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.toggle(event.option_index)

    def _redraw(self, index: int) -> None:
        self.replace_prompt(index, self._prompt(index))

    def _prompt(self, index: int) -> GutteredEntry:
        # Partial styles, so no background of their own paints a band across the entry.
        text, kind = self._entries[index]
        marked = index == self._marked
        style = self.get_component_rich_style(f"transcript--{kind}", partial=True)
        gutter_style = self.get_component_rich_style(
            "transcript--marker" if marked else "transcript--gutter", partial=True
        )
        label = Text(entry_label(text, self._collapsed[index]), style=style)
        return GutteredEntry(label, gutter(marked), gutter_style)


class Transcript(Vertical):
    """Main display: the running conversation, pinned to the latest output unless the shown
    tab's log holds a cursor.

    Entries are kept per tab (All, and the tab they belong to), each tab in a
    :class:`TranscriptLog` of its own, one of which is shown.
    """

    # Windows are reached with h/j/k/l, not by clicking; the mouse wheel still scrolls.
    can_focus = False

    DEFAULT_CSS = """
    Transcript {
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.only: str | None = None  # Show only this tab's entries, or all.
        self._logs: dict[str | None, TranscriptLog] = {}

    def compose(self) -> ComposeResult:
        yield self._new_log(None, shown=True)

    def on_mount(self) -> None:
        self.border_title = "Conversation"

    @property
    def shown(self) -> TranscriptLog:
        """The log of the tab being shown."""
        return self._logs[self.only]

    def entries(self, key: str | None) -> tuple[Entry, ...]:
        """The entries under the tab keyed ``key``; None is All."""
        return self._logs[key].entries if key in self._logs else ()

    def add_entry(self, text: str, kind: EntryKind, key: str | None = None) -> None:
        """Add an entry under All and, if ``key`` names a tab, under that tab."""
        for tab in {None, key}:
            self._log(tab).append(text, kind)

    def enter_list_mode(self) -> None:
        """Activate the shown log: j/k move between its entries, enter collapses or expands
        one, and escape leaves."""
        self.shown.enter_list_mode()

    def release_cursor(self) -> None:
        """Let go of the shown log's cursor, so it follows the latest entry again."""
        self.shown.release_cursor()

    def show_only(self, key: str | None, label: str = "") -> None:
        """Show only the tab keyed ``key``'s entries, titled by its tab ``label``, or all."""
        if self.shown.has_focus:
            self.screen.set_focus(None)
        self.shown.remove_class("-shown")
        self.only = key
        self.border_title = "Conversation" if key is None else f"Conversation · {label}"
        log = self._log(key)
        log.add_class("-shown")
        # Laid out once shown, which is when its end is known.
        self.call_after_refresh(log.show_place)

    def _log(self, key: str | None) -> TranscriptLog:
        if key not in self._logs:
            self.mount(self._new_log(key))
        return self._logs[key]

    def _new_log(self, key: str | None, shown: bool = False) -> TranscriptLog:
        log = TranscriptLog(shown=shown)
        self._logs[key] = log
        return log
