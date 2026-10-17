"""The conversation window: the transcript and its per-tab logs."""

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, ClassVar, get_args

from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.rule import Rule
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.timer import Timer
from textual.widgets import OptionList, Static

from orbit.protocol import Event
from orbit.tui.view import (
    SPINNER_FPS,
    EntryKind,
    Working,
    WorkingView,
    banded,
    collapsible,
    conversation_title,
    entry_label,
    entry_markdown,
    gutter,
    hanging_indent,
    ruled,
    shows_markdown,
    speaker_label,
    starts_collapsed,
    track_working,
    working_lines,
)
from orbit.tui.widgets import ListPane


@dataclass(frozen=True)
class Entry:
    """A transcript entry: its ``text``, the ``kind`` it's styled as and the ``agent`` it's
    by, if any. An agent's message thinking out loud keeps the ``turn`` it's from, and is drawn
    dim, until it's found to be the turn's final answer."""

    text: str
    kind: EntryKind
    agent: str | None = None
    turn: str | None = None


@dataclass(frozen=True)
class HangingIndent:
    """``body`` drawn in the width left beside its indent: ``first`` in front of its first row
    and ``rest`` in front of every row after it, both in ``style``."""

    body: RenderableType
    first: str
    rest: str
    style: Style

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = options.max_width - len(self.first)
        rows = console.render_lines(self.body, options.update_width(width), pad=False)
        for index, row in enumerate(rows):
            yield Segment(self.rest if index else self.first, self.style)
            yield from row
            yield Segment.line()


@dataclass(frozen=True)
class GutteredEntry:
    """A transcript entry as drawn: its ``heading`` if it has one, then its ``body``, or its
    ``label`` when it has none, wrapped to the width left beside the ``gutter``, which goes in
    front of every row in ``gutter_style``, then a blank row so the entry reads as a
    paragraph. With a ``band``, every row is filled to its end in that style."""

    label: Text
    gutter: str
    gutter_style: Style
    body: RenderableType | None = None
    heading: Text | None = None
    band: Style | None = None

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = options.max_width - len(self.gutter)
        shown = self.label if self.body is None else self.body
        parts = (shown,) if self.heading is None else (self.heading, shown)
        for part in parts:
            rows = console.render_lines(
                part, options.update_width(width), style=self.band, pad=self.band is not None
            )
            for row in rows:
                yield Segment(self.gutter, self.gutter_style)
                yield from row
                yield Segment.line()
        yield Segment.line()


def markdown_body(kind: EntryKind, text: str, style: Style) -> HangingIndent:
    """``text`` drawn as markdown in ``style``, the prefix and marker of an entry of ``kind``
    hanging beside it if it has them."""
    first, rest = hanging_indent(kind, text)
    return HangingIndent(entry_markdown(text, style), first, rest, style)


class TranscriptLog(ListPane):
    """One tab's entries, each an option: activated, j/k move between them and enter collapses
    or expands one. Kept at no display until shown, since the list lays out only what's on
    screen, so switching tabs is instant and adding an entry costs the same however long the
    conversation gets. The cursor's entry is marked in the left gutter rather than highlighted.
    Hiding thinking leaves agents' dim messages out of the options; the entries are all kept,
    and option indexes count only the entries shown."""

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
    TranscriptLog .transcript--prompt { background: $foreground 5%; }
    TranscriptLog .transcript--tool { color: $text-muted; }
    TranscriptLog .transcript--verify { color: $text-warning; }
    TranscriptLog .transcript--agent { color: $text-accent; }
    TranscriptLog .transcript--request { color: $text-accent; }
    TranscriptLog .transcript--error { color: $text-error; }
    TranscriptLog .transcript--gutter { color: $text-muted; }
    TranscriptLog .transcript--marker { color: $accent; }
    TranscriptLog .transcript--label { color: $text-muted; }
    TranscriptLog .transcript--thinking { color: $text-muted; }
    TranscriptLog .transcript--rule { color: $text-muted; }
    TranscriptLog .transcript--error_rule { color: $text-error; }
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
        "transcript--label",
        "transcript--thinking",
        *(f"transcript--{kind}" for kind in get_args(EntryKind)),
    }

    def __init__(self, shown: bool = False, thinking_hidden: bool = False) -> None:
        super().__init__()
        self._entries: list[Entry] = []
        self._collapsed: list[bool] = []
        self._rows: list[int] = []  # The entry each option shows.
        self._marked: int | None = None  # The option drawn with the marker.
        self._thinking_hidden = thinking_hidden
        self.set_class(shown, "-shown")

    @property
    def entries(self) -> tuple[Entry, ...]:
        return tuple(self._entries)

    def enter_list_mode(self) -> None:
        """Activate the list, starting at the latest entry when it holds no cursor."""
        if self.highlighted is None and self.option_count:
            self.highlighted = self.option_count - 1
        super().enter_list_mode()

    def append(
        self, text: str, kind: EntryKind, agent: str | None = None, turn: str | None = None
    ) -> None:
        """Add an entry by ``agent``, dim while thinking out loud in ``turn``, collapsed if its
        kind starts so; the view stays pinned to the latest entry unless the list is active or
        holds a cursor."""
        collapsed = starts_collapsed(kind, text)
        entry = Entry(text, kind, agent, turn)
        self._entries.append(entry)
        self._collapsed.append(collapsed)
        if not self._shows(entry):
            return
        self._rows.append(len(self._entries) - 1)
        self.add_option(self._prompt(len(self._rows) - 1))
        if not self.has_focus and self.highlighted is None:
            self.scroll_end(animate=False)

    def replace_rule(self, agent: str, text: str, kind: EntryKind) -> None:
        """Make ``agent``'s latest rule read ``text`` as a ``kind`` entry, redrawn where it
        is; a log without one is left as it is."""
        rules = [
            i for i, entry in enumerate(self._entries) if entry.agent == agent and ruled(entry.kind)
        ]
        if not rules:
            return
        index = rules[-1]
        self._entries[index] = Entry(text, kind, agent)
        self._redraw(self._rows.index(index))  # Rules are never hidden.

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

    def promote(self, agent: str | None, turn: str, places: Iterable[int]) -> None:
        """Draw at full brightness the messages ``agent`` thought out loud in ``turn`` at
        ``places`` among them, redrawing only those, or showing them if thinking is hidden; a log
        without them is left as it is."""
        thinking = self._thinking(agent, turn)
        promoted = [thinking[place] for place in places if place < len(thinking)]
        for index in promoted:
            self._entries[index] = replace(self._entries[index], turn=None)
        self._show_promoted(promoted)

    def _show_promoted(self, promoted: list[int]) -> None:
        """Show the entries at ``promoted``, just brightened: redrawn where they were, or
        added among the options when thinking was hidden."""
        if not self._thinking_hidden:
            for index in promoted:  # Every entry is shown, each at its own option.
                self._redraw(index)
        elif promoted:
            self._show_rows()

    def hide_thinking(self, hidden: bool) -> None:
        """Leave out agents' dim messages if ``hidden``, or show them again, the cursor moving
        to the nearest entry still shown when its own is hidden."""
        self._thinking_hidden = hidden
        self._show_rows()

    def _shows(self, entry: Entry) -> bool:
        return not (self._thinking_hidden and entry.turn is not None)

    def _show_rows(self) -> None:
        """Rebuild the options from the entries shown, keeping the cursor's place."""
        held = None if self.highlighted is None else self._rows[self.highlighted]
        self._rows = [i for i, entry in enumerate(self._entries) if self._shows(entry)]
        self._marked = None  # Its option is about to go.
        self.clear_options()
        self.add_options([self._prompt(row) for row in range(len(self._rows))])
        self.highlighted = None if held is None else self._nearest_row(held)
        self.call_after_refresh(self.show_place)

    def _nearest_row(self, index: int) -> int | None:
        """The option of the entry at ``index``, or of the nearest shown entry before it, else
        after it; None when none is shown."""
        before = [row for row, shown in enumerate(self._rows) if shown <= index]
        if before:
            return before[-1]
        return 0 if self._rows else None

    def _thinking(self, agent: str | None, turn: str) -> list[int]:
        """Where the messages ``agent`` is thinking out loud in ``turn`` are, in order."""
        entries = self._entries
        return [i for i, entry in enumerate(entries) if (entry.agent, entry.turn) == (agent, turn)]

    def collapsed(self, row: int) -> bool:
        """Whether the entry at option ``row`` is collapsed."""
        return self._collapsed[self._rows[row]]

    def toggle(self, row: int) -> None:
        """Collapse the entry at option ``row`` if it's expanded, and the other way round; an
        entry of one line stays as it is."""
        index = self._rows[row]
        if not collapsible(self._entries[index].text):
            return
        self._collapsed[index] = not self._collapsed[index]
        self._redraw(row)

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

    def _redraw(self, row: int) -> None:
        self.replace_prompt(row, self._prompt(row))

    def _prompt(self, row: int) -> GutteredEntry:
        # Partial styles, so no background of their own paints a band across the entry; a
        # banded kind's full style, whose translucent background is blended with the log's.
        index = self._rows[row]
        entry = self._entries[index]
        marked = row == self._marked
        band = banded(entry.kind)
        component = "thinking" if entry.turn is not None else entry.kind
        style = self.get_component_rich_style(f"transcript--{component}", partial=not band)
        gutter_style = self.get_component_rich_style(
            "transcript--marker" if marked else "transcript--gutter", partial=True
        )
        collapsed = self._collapsed[index]
        label = Text(entry_label(entry.kind, entry.text, collapsed), style=style)
        body = self._body(entry, collapsed, style)
        heading = self._heading(row)
        return GutteredEntry(
            label, gutter(marked), gutter_style, body, heading, style if band else None
        )

    @staticmethod
    def _body(entry: Entry, collapsed: bool, style: Style) -> RenderableType | None:
        """What the entry draws instead of its label: a rule, or markdown, if it does."""
        if ruled(entry.kind):
            return Rule(Text(entry.text, style=style), style=style)
        if shows_markdown(entry.kind, collapsed):
            return markdown_body(entry.kind, entry.text, style)
        return None

    def _heading(self, row: int) -> Text | None:
        """The label row heading the entry at option ``row``, when it's an agent's message and
        the speaker changed from the entry shown before it."""
        entry = self._entries[self._rows[row]]
        previous = self._entries[self._rows[row - 1]].agent if row else None
        label = speaker_label(entry.kind, entry.agent, previous)
        if label is None:
            return None
        return Text(label, style=self.get_component_rich_style("transcript--label", partial=True))


class WorkingRow(Static):
    """What's running right now under the shown tab, one line per agent, docked below the
    logs; hidden while nothing shown runs. Its spinners and clocks tick only while something
    runs."""

    DEFAULT_CSS = """
    WorkingRow {
        dock: bottom;
        display: none;
        height: auto;
        color: $text-muted;
    }
    WorkingRow.-running { display: block; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.clock: Callable[[], float] = time.monotonic
        self.working: Working = ()
        self.lines: tuple[str, ...] = ()
        self._roles: Mapping[str, str] = {}
        self._shown: str | None = None
        self._timer: Timer | None = None

    @property
    def ticking(self) -> bool:
        """Whether the spinners are turning."""
        return self._timer is not None

    def track(self, event: Event, roles: Mapping[str, str]) -> None:
        """Follow who runs after ``event``, with ``roles`` placing agents in tabs."""
        self.working = track_working(self.working, event, self.clock())
        self._roles = roles
        if self.working and self._timer is None:
            self._timer = self.set_interval(1 / SPINNER_FPS, self.tick)
        elif not self.working and self._timer is not None:
            self._timer.stop()
            self._timer = None
        self.tick()

    def show_tab(self, key: str | None) -> None:
        """Show what runs under the tab keyed ``key``; None is All."""
        self._shown = key
        self.tick()

    def on_resize(self) -> None:
        self.tick()

    def tick(self) -> None:
        """Redraw the lines, if they changed."""
        view = WorkingView(self.working, self._roles, self._shown)
        lines = working_lines(view, self.clock(), self.size.width)
        if lines == self.lines:
            return
        self.lines = lines
        self.update("\n".join(lines))
        self.set_class(bool(lines), "-running")


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
        self._label: str | None = None  # The shown tab's label; None for All.
        self.thinking_hidden = False
        self._logs: dict[str | None, TranscriptLog] = {}

    def compose(self) -> ComposeResult:
        yield WorkingRow()
        yield self._new_log(None, shown=True)

    def on_mount(self) -> None:
        self._retitle()

    @property
    def shown(self) -> TranscriptLog:
        """The log of the tab being shown."""
        return self._logs[self.only]

    @property
    def working(self) -> WorkingRow:
        """The row of what's running, below the logs."""
        return self.query_one(WorkingRow)

    def track(self, event: Event, roles: Mapping[str, str]) -> None:
        """Follow who runs after ``event``, with ``roles`` placing agents in tabs."""
        self.working.track(event, roles)

    def entries(self, key: str | None) -> tuple[Entry, ...]:
        """The entries under the tab keyed ``key``; None is All."""
        return self._logs[key].entries if key in self._logs else ()

    def add_entry(
        self, text: str, kind: EntryKind, key: str | None = None, agent: str | None = None
    ) -> None:
        """Add an entry by ``agent`` under All and, if ``key`` names a tab, under that tab."""
        for tab in {None, key}:
            self._log(tab).append(text, kind, agent)

    def replace_rule(self, text: str, kind: EntryKind, key: str | None, agent: str) -> None:
        """Make ``agent``'s latest rule read ``text`` as a ``kind`` entry, under All and, if
        ``key`` names a tab, under that tab."""
        for tab in {None, key}:
            self._log(tab).replace_rule(agent, text, kind)

    def add_thinking(self, text: str, key: str | None, agent: str | None, turn: str) -> None:
        """Add ``agent``'s message in ``turn``, dim as thinking out loud until it's promoted,
        under All and, if ``key`` names a tab, under that tab."""
        for tab in {None, key}:
            self._log(tab).append(text, "assistant", agent, turn)

    def promote(self, agent: str | None, turn: str, places: Iterable[int]) -> None:
        """Draw at full brightness, in every log holding them, the messages ``agent`` thought
        out loud in ``turn`` at ``places`` among them."""
        shown = tuple(places)
        for log in self._logs.values():
            log.promote(agent, turn, shown)

    def toggle_thinking(self) -> None:
        """Hide agents' dim messages in every tab, or show them again."""
        self.thinking_hidden = not self.thinking_hidden
        for log in self._logs.values():
            log.hide_thinking(self.thinking_hidden)
        self._retitle()

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
        self._label = None if key is None else label
        self._retitle()
        self.working.show_tab(key)
        log = self._log(key)
        log.add_class("-shown")
        # Laid out once shown, which is when its end is known.
        self.call_after_refresh(log.show_place)

    def _retitle(self) -> None:
        self.border_title = conversation_title(self._label, self.thinking_hidden)

    def _log(self, key: str | None) -> TranscriptLog:
        if key not in self._logs:
            self.mount(self._new_log(key))
        return self._logs[key]

    def _new_log(self, key: str | None, shown: bool = False) -> TranscriptLog:
        log = TranscriptLog(shown=shown, thinking_hidden=self.thinking_hidden)
        self._logs[key] = log
        return log
