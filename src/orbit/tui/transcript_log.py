"""One tab's entries in the conversation window, as a list pane."""

import time
from collections.abc import Callable, Iterable, Sequence
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
from orbit.tui.view.log import Entry, EntryLog, EntryRow
from orbit.tui.view.tool_calls import ToolCall


class TranscriptLog(ListPane):
    """Shows one tab's :class:`EntryLog`, each entry an option: activated, j/k move between
    them and enter collapses or expands one, or inspects a tool call or request. Kept at no
    display until shown, since the list lays out only what's on screen, so switching tabs is
    instant and adding an entry costs the same however long the conversation gets. The
    cursor's entry is marked in the left gutter rather than highlighted. Moving up past the
    first entry asks for earlier ones."""

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
        self.entry_log = EntryLog(self, thinking_hidden, clock)
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
        return self.entry_log.entries

    @property
    def inspected(self) -> int | None:
        """Where the cursor's entry is among the entries, if there's a cursor."""
        return self.entry_log.inspected

    @property
    def detail(self) -> ToolCall | None:
        """The tool call or request of the cursor's entry, if it's one."""
        return self.entry_log.detail

    @property
    def inspecting(self) -> bool:
        """Whether the detail pane shows the cursor's entry."""
        return self.entry_log.inspecting

    @inspecting.setter
    def inspecting(self, inspecting: bool) -> None:
        self.entry_log.inspecting = inspecting

    def action_normal_mode(self) -> None:
        """Stop inspecting if the log is, else leave list mode."""
        if not self.entry_log.stop_inspecting():
            super().action_normal_mode()

    def on_blur(self) -> None:
        """Leave list mode, and stop inspecting unless the detail pane took focus."""
        super().on_blur()
        self.entry_log.blurred(isinstance(self.screen.focused, DetailPane))

    def action_cursor_up(self) -> None:
        """Move the cursor to the entry above; on the first, it stays, asking for earlier
        ones."""
        if self.entry_log.at_top():
            self.post_message(self.ReachedTop())
        else:
            super().action_cursor_up()

    def enter_list_mode(self) -> None:
        """Activate the list, starting at the latest entry when it holds no cursor."""
        self.entry_log.start_at_latest()
        super().enter_list_mode()

    def append(self, entry: Entry) -> None:
        """Add ``entry``, as the entry log adds it."""
        self.entry_log.append(entry)

    def clear(self) -> None:
        """Drop every entry."""
        self.entry_log.clear()

    def begin_earlier(self) -> None:
        """Set the entries aside, so those added until :meth:`end_earlier` come before them."""
        self.entry_log.begin_earlier()

    def end_earlier(self) -> None:
        """Put the entries set aside back after those added since."""
        self.entry_log.end_earlier()

    def drop(self, entry: Entry) -> None:
        """Drop ``entry``, as the entry log drops it."""
        self.entry_log.drop(entry)

    def replace_rule(self, agent: str, text: str, kind: EntryKind) -> None:
        """Make ``agent``'s latest rule read ``text`` as a ``kind`` entry."""
        self.entry_log.replace_rule(agent, text, kind)

    @property
    def running(self) -> bool:
        """Whether any tool call shown here is still running."""
        return self.entry_log.running

    def append_call(self, call: ToolCall, agent: str | None, speaker: str | None) -> None:
        """Add ``agent``'s running ``call``, spoken by ``speaker``, as a one-line entry."""
        self.entry_log.append_call(call, agent, speaker)

    def finish_call(self, event: ToolCallFinished | AgentReply) -> bool:
        """Show the running call or request ``event`` finishes as finished; False when no
        running call here is the one."""
        return self.entry_log.finish_call(event)

    def end_calls(self, agent: str | None, requests: bool) -> None:
        """Show ``agent``'s tool calls still running as having no result."""
        self.entry_log.end_calls(agent, requests)

    def end_running_calls(self) -> None:
        """Show every call and request still running as having no result."""
        self.entry_log.end_running_calls()

    def tick(self) -> None:
        """Turn the spinners."""
        self.entry_log.tick()

    def release_cursor(self) -> None:
        """Let go of the held cursor, so the view follows the latest entry again."""
        self.entry_log.release_cursor()

    def show_place(self) -> None:
        """Scroll to the held cursor's entry, or to the latest entry if there's none."""
        if self.highlighted is None:
            self.scroll_end(animate=False)
        else:
            self.scroll_to_highlight()

    def promote(self, agent: str | None, turn: str, places: Iterable[int]) -> None:
        """Draw at full brightness the messages ``agent`` thought out loud in ``turn`` at
        ``places`` among them."""
        self.entry_log.promote(agent, turn, places)

    def hide_thinking(self, hidden: bool) -> None:
        """Leave out agents' dim messages if ``hidden``, or show them again."""
        self.entry_log.hide_thinking(hidden)

    def collapsed(self, row: int) -> bool:
        """Whether the entry at option ``row`` is collapsed."""
        return self.entry_log.collapsed(row)

    def toggle(self, row: int) -> None:
        """Collapse the entry at option ``row`` if it's expanded, and the other way round."""
        self.entry_log.toggle(row)

    def watch_highlighted(self, highlighted: int | None) -> None:
        """Move the marker from the entry it was on to the cursor's, if there is one."""
        super().watch_highlighted(highlighted)
        self.entry_log.moved(highlighted)

    @on(OptionList.OptionSelected)
    def _select(self, event: OptionList.OptionSelected) -> None:
        """Inspect a tool call or request; collapse or expand any other entry."""
        event.stop()
        self.entry_log.choose(event.option_index)

    def highlight(self, row: int | None) -> None:
        self.highlighted = row

    def add_row(self, row: EntryRow) -> None:
        self.add_option(self._prompt(row))

    def redraw(self, index: int, row: EntryRow) -> None:
        self.replace_prompt(index, self._prompt(row))

    def replace_rows(self, rows: Sequence[EntryRow], highlight: int | None) -> None:
        self.clear_options()
        self.add_options([self._prompt(row) for row in rows])
        self.highlighted = highlight
        self.call_after_refresh(self.show_place)

    def clear_rows(self) -> None:
        self.clear_options()

    def scroll_to_end(self) -> None:
        self.scroll_end(animate=False)

    def detail_changed(self) -> None:
        self.post_message(self.Inspect())

    def _prompt(self, row: EntryRow) -> GutteredEntry:
        # Partial styles, so no background of their own paints a band across the entry; a
        # banded kind's full style, whose translucent background is blended with the log's.
        style = self.get_component_rich_style(row.component, partial=not row.banded)
        gutter_style = self.get_component_rich_style(row.gutter_component, partial=True)
        label = Text(row.label, style=style)
        heading = None if row.heading is None else Text(row.heading, style=self._label_style)
        band = style if row.banded else None
        return GutteredEntry(label, row.gutter, gutter_style, self._body(row, style), heading, band)

    @property
    def _label_style(self) -> Style:
        return self.get_component_rich_style("transcript--label", partial=True)

    @staticmethod
    def _body(row: EntryRow, style: Style) -> RenderableType | None:
        """What the entry draws instead of its label: a rule, or markdown, if it does."""
        if row.body == "rule":
            return Rule(Text(row.text, style=style), style=style)
        if row.body == "markdown":
            return markdown_body(row.kind, row.text, style)
        return None
