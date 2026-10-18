"""One tab's entries in the conversation window, as a list pane."""

import time
from bisect import bisect_left
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import ClassVar, get_args

from rich.console import RenderableType
from rich.rule import Rule
from rich.style import Style
from rich.text import Text
from textual import on
from textual.message import Message
from textual.widgets import OptionList

from orbit.protocol import AgentReply, ToolCallFinished
from orbit.tui.detail import DetailPane
from orbit.tui.entries import GutteredEntry, markdown_body
from orbit.tui.list_pane import ListPane
from orbit.tui.view.common import EntryKind
from orbit.tui.view.log import (
    Entry,
    after_rows,
    latest_rule,
    nearest_row,
    shown,
    shown_rows,
    thinking_places,
)
from orbit.tui.view.tool_calls import ToolCall, finishes, tool_call_finished, tool_call_unfinished
from orbit.tui.view.transcript import (
    banded,
    collapsible,
    entry_label,
    gutter,
    ruled,
    shows_markdown,
    speaker_label,
    starts_collapsed,
    tool_call_entry,
)

type _SetAside = tuple[list[Entry], list[bool], dict[int, ToolCall], int | None]
"""A log's entries, whether each is collapsed, its running calls and its cursor's entry."""


class TranscriptLog(ListPane):
    """One tab's entries, each an option: activated, j/k move between them and enter collapses
    or expands one. Kept at no display until shown, since the list lays out only what's on
    screen, so switching tabs is instant and adding an entry costs the same however long the
    conversation gets. The cursor's entry is marked in the left gutter rather than highlighted.
    Hiding thinking leaves agents' dim messages out of the options; the entries are all kept,
    and option indexes count only the entries shown. A tool call is one line, updated in place
    as it finishes, its spinner read off ``clock``. Inspecting, enter on a tool call or
    request asks for its details, which follow the cursor until escape. Moving up past the
    first entry asks for earlier ones, which :meth:`begin_earlier` and :meth:`end_earlier` add
    above it."""

    class Inspect(Message):
        """What the detail pane should show may have changed: whether the log is
        :attr:`inspecting`, or its :attr:`detail`."""

    class ReachedTop(Message):
        """The cursor was moved up past the first entry."""

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

    def __init__(
        self,
        shown: bool = False,
        thinking_hidden: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self.clock = clock
        self._entries: list[Entry] = []
        self._collapsed: list[bool] = []
        self._rows: list[int] = []  # The entry each option shows.
        self._marked: int | None = None  # The option drawn with the marker.
        self._calls: dict[int, ToolCall] = {}  # The calls still running, by their entry.
        self._later: _SetAside | None = None  # Set aside while earlier entries are added.
        self._thinking_hidden = thinking_hidden
        self.inspecting = False  # Whether the detail pane shows the cursor's entry.
        self.display = shown
        self.set_class(shown, "-shown")

    def show(self, shown: bool) -> None:
        """Display the log if ``shown``, marked by the ``-shown`` class, or hide it."""
        # Displayed directly rather than by the class, as a class change restyles all the
        # log's component classes.
        self.display = shown
        self.set_class(shown, "-shown", update=False)

    @property
    def entries(self) -> tuple[Entry, ...]:
        return tuple(self._entries)

    @property
    def inspected(self) -> int | None:
        """Where the cursor's entry is among the entries, if there's a cursor."""
        return None if self.highlighted is None else self._rows[self.highlighted]

    @property
    def detail(self) -> ToolCall | None:
        """The tool call or request of the cursor's entry, if it's one."""
        index = self.inspected
        return None if index is None else self._entries[index].call

    def inspect(self, inspecting: bool) -> None:
        """Show the cursor's entry's details as it moves if ``inspecting``, or stop."""
        self.inspecting = inspecting
        self.post_message(self.Inspect())

    def action_normal_mode(self) -> None:
        """Stop inspecting if the log is, else leave list mode."""
        if self.inspecting:
            self.inspect(False)
        else:
            super().action_normal_mode()

    def on_blur(self) -> None:
        """Leave list mode and, unless the detail pane took focus, stop inspecting: the pane
        only follows a log in list mode."""
        super().on_blur()
        if self.inspecting and not isinstance(self.screen.focused, DetailPane):
            self.inspect(False)

    def action_cursor_up(self) -> None:
        """Move the cursor to the entry above; on the first, it stays, asking for earlier
        ones."""
        if self.highlighted == 0:
            self.post_message(self.ReachedTop())
        else:
            super().action_cursor_up()

    def enter_list_mode(self) -> None:
        """Activate the list, starting at the latest entry when it holds no cursor."""
        if self.highlighted is None and self.option_count:
            self.highlighted = self.option_count - 1
        super().enter_list_mode()

    def append(self, entry: Entry) -> None:
        """Add ``entry``, dim while its agent thinks out loud in its turn, collapsed if its
        kind starts so; the view stays pinned to the latest entry unless the list is active or
        holds a cursor."""
        self._entries.append(entry)
        self._collapsed.append(starts_collapsed(entry.kind, entry.text))
        if shown(entry, self._thinking_hidden):
            self._show_after(len(self._entries) - 1)

    def clear(self) -> None:
        """Drop every entry."""
        self._entries.clear()
        self._collapsed.clear()
        self._rows.clear()
        self._marked = None
        self._calls.clear()
        self.clear_options()

    def begin_earlier(self) -> None:
        """Set the entries aside, so those added until :meth:`end_earlier` come before them."""
        self._later = (self._entries, self._collapsed, self._calls, self.inspected)
        self._entries, self._collapsed, self._calls = [], [], {}
        self._rows, self._marked = [], None
        self.clear_options()

    def end_earlier(self) -> None:
        """Put the entries set aside back after those added since, whose calls still running
        are shown as having no result, the cursor kept on the entry it was on."""
        assert self._later is not None  # Only ever after begin_earlier.
        entries, collapsed, calls, held = self._later
        self._later = None
        for index, call in list(self._calls.items()):
            self._set_call(index, tool_call_unfinished(call))
        offset = len(self._entries)
        self._entries += entries
        self._collapsed += collapsed
        self._calls = {index + offset: call for index, call in calls.items()}
        self._show_rows_holding(None if held is None else held + offset)

    def _show_after(self, index: int) -> None:
        """Add the entry at ``index``, which comes after every entry shown, as the last option,
        the view pinned to it unless the list is active or holds a cursor."""
        self._rows.append(index)
        self.add_option(self._prompt(len(self._rows) - 1))
        if not self.has_focus and self.highlighted is None:
            self.scroll_end(animate=False)

    def replace_rule(self, agent: str, text: str, kind: EntryKind) -> None:
        """Make ``agent``'s latest rule read ``text`` as a ``kind`` entry, redrawn where it
        is; a log without one is left as it is."""
        index = latest_rule(self._entries, agent)
        if index is None:
            return
        self._entries[index] = replace(self._entries[index], text=text, kind=kind)
        self._redraw(self._rows.index(index))  # Rules are never hidden.

    @property
    def running(self) -> bool:
        """Whether any tool call shown here is still running."""
        return bool(self._calls)

    def append_call(self, call: ToolCall, agent: str | None, speaker: str | None) -> None:
        """Add ``agent``'s running ``call``, spoken by ``speaker``, as a one-line entry, its
        spinner turning on :meth:`tick`."""
        self._calls[len(self._entries)] = call
        text, kind = tool_call_entry(call, agent, self.clock())
        self.append(Entry(text, kind, agent, call=call, speaker=speaker))

    def finish_call(self, event: ToolCallFinished | AgentReply) -> bool:
        """Show the running call or request ``event`` finishes as finished, redrawn where it
        is; False, and nothing changed, when no running call here is the one."""
        for index, call in self._calls.items():
            if finishes(event, call, self._entries[index].agent):
                self._set_call(index, tool_call_finished(call, event))
                return True
        return False

    def end_calls(self, agent: str | None, requests: bool) -> None:
        """Show ``agent``'s tool calls still running as having no result, their turn over, and
        its requests to orbit with no reply too if ``requests``, the agent gone."""
        for index, call in list(self._calls.items()):
            if self._entries[index].agent == agent and (requests or not call.request):
                self._set_call(index, tool_call_unfinished(call))

    def tick(self) -> None:
        """Turn the spinners: redraw just the entries of running calls."""
        for index, call in list(self._calls.items()):
            self._set_call(index, call)

    def _set_call(self, index: int, call: ToolCall) -> None:
        """Make the entry at ``index`` show ``call``, redrawn where it is if its text changed;
        a call no longer running stops turning."""
        if call.status != "running":
            del self._calls[index]
        entry = self._entries[index]
        text, kind = tool_call_entry(call, entry.agent, self.clock())
        self._entries[index] = replace(entry, text=text, kind=kind, call=call)
        if self.inspecting and call != entry.call and self.inspected == index:
            self.post_message(self.Inspect())
        if (text, kind) != (entry.text, entry.kind):
            self._redraw(bisect_left(self._rows, index))  # Tool calls are never hidden.

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
        thinking = thinking_places(self._entries, agent, turn)
        promoted = [thinking[place] for place in places if place < len(thinking)]
        for index in promoted:
            self._entries[index] = replace(self._entries[index], turn=None)
        self._show_promoted(promoted)

    def _show_promoted(self, promoted: list[int]) -> None:
        """Show the entries at ``promoted``, just brightened: redrawn where they were, or
        added among the options when thinking was hidden: after them if they all come after
        the last entry shown, else by rebuilding the options."""
        if not self._thinking_hidden:
            for index in promoted:  # Every entry is shown, each at its own option.
                self._redraw(index)
        elif after_rows(self._rows, promoted):
            for index in sorted(promoted):
                self._show_after(index)
        else:
            self._show_rows()

    def hide_thinking(self, hidden: bool) -> None:
        """Leave out agents' dim messages if ``hidden``, or show them again, the cursor moving
        to the nearest entry still shown when its own is hidden."""
        self._thinking_hidden = hidden
        self._show_rows()

    def _show_rows(self) -> None:
        """Rebuild the options from the entries shown, keeping the cursor's place."""
        self._show_rows_holding(None if self.highlighted is None else self._rows[self.highlighted])

    def _show_rows_holding(self, held: int | None) -> None:
        """Rebuild the options from the entries shown, the cursor on the entry at ``held``, or
        the nearest one shown, if given."""
        self._rows = shown_rows(self._entries, self._thinking_hidden)
        self._marked = None  # Its option is about to go.
        self.clear_options()
        self.add_options([self._prompt(row) for row in range(len(self._rows))])
        self.highlighted = None if held is None else nearest_row(self._rows, held)
        self.call_after_refresh(self.show_place)

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
        if self.inspecting:
            self.post_message(self.Inspect())

    @on(OptionList.OptionSelected)
    def _select(self, event: OptionList.OptionSelected) -> None:
        """Inspect a tool call or request; collapse or expand any other entry."""
        event.stop()
        if self._entries[self._rows[event.option_index]].call is None:
            self.toggle(event.option_index)
        else:
            self.inspect(True)

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
        previous = self._entries[self._rows[row - 1]].speaker if row else None
        label = speaker_label(entry.kind, entry.speaker, previous)
        if label is None:
            return None
        return Text(label, style=self.get_component_rich_style("transcript--label", partial=True))
