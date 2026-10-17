"""The conversation window: the transcript and its per-tab logs."""

import time
from bisect import bisect_left
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, ClassVar, get_args

from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.markdown import Heading, Markdown, MarkdownContext, MarkdownElement
from rich.rule import Rule
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import OptionList, Static

from orbit.protocol import AgentReply, AgentRequest, Event, ToolCallFinished, ToolCallStarted
from orbit.tui.view import (
    DETAIL_HEIGHT,
    SPINNER_FPS,
    EntryKind,
    ToolCall,
    Working,
    WorkingView,
    banded,
    collapsible,
    conversation_title,
    copied_note,
    detail_body,
    detail_copy,
    detail_covers,
    detail_title,
    entry_label,
    finishes,
    gutter,
    hanging_indent,
    ruled,
    shows_markdown,
    speaker_label,
    starts_collapsed,
    tool_call_entry,
    tool_call_finished,
    tool_call_started,
    tool_call_unfinished,
    track_working,
    working_lines,
)
from orbit.tui.widgets import ListPane, ModalFocus


@dataclass(frozen=True)
class Entry:
    """A transcript entry: its ``text``, the ``kind`` it's styled as, the ``agent`` it's by,
    if any, and the ``speaker`` it's spoken by (see :func:`speaker`). An agent's message thinking
    out loud keeps the ``turn`` it's from, and is drawn dim, until it's found to be the turn's
    final answer. A tool call's entry keeps the ``call``, which its text shows in a line."""

    text: str
    kind: EntryKind
    agent: str | None = None
    turn: str | None = None
    call: ToolCall | None = None
    speaker: str | None = None


class LeftHeading(Heading):
    """A heading drawn bold and left-aligned, h1 also underlined, rather than centred."""

    def on_enter(self, context: MarkdownContext) -> None:
        self.text = Text()
        context.enter_style(Style(bold=True, underline=self.tag == "h1"))

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        text = self.text.copy()
        text.justify = "left"
        yield text


class EntryMarkdown(Markdown):
    """Markdown as an entry shows it: headings drawn as :class:`LeftHeading`."""

    elements: ClassVar[dict[str, type[MarkdownElement]]] = {
        **Markdown.elements,
        "heading_open": LeftHeading,
    }


def entry_markdown(text: str, style: Style) -> Markdown:
    """``text`` as markdown in the entry's ``style``, its links shown as ``text (url)``."""
    return EntryMarkdown(text, style=style, hyperlinks=False)


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

    def _rows(self, console: Console, options: ConsoleOptions) -> list[list[Segment]]:
        """The rows of the entry's heading and body, drawn in ``options``' width."""
        shown = self.label if self.body is None else self.body
        parts = (shown,) if self.heading is None else (self.heading, shown)
        return [
            row
            for part in parts
            for row in console.render_lines(
                part, options, style=self.band, pad=self.band is not None
            )
        ]

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = options.max_width - len(self.gutter)
        rows = self._rows(console, options.update_width(width))
        # Markdown that renders nothing, such as only whitespace, still gets a row to mark.
        for row in rows or [[]]:
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
    and option indexes count only the entries shown. A tool call is one line, updated in place
    as it finishes, its spinner read off ``clock``. Inspecting, enter on a tool call or
    request asks for its details, which follow the cursor until escape."""

    class Inspect(Message):
        """What the detail pane should show may have changed: whether the log is
        :attr:`inspecting`, or its :attr:`detail`."""

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
        self._thinking_hidden = thinking_hidden
        self.inspecting = False  # Whether the detail pane shows the cursor's entry.
        self.set_class(shown, "-shown")

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

    def enter_list_mode(self) -> None:
        """Activate the list, starting at the latest entry when it holds no cursor."""
        if self.highlighted is None and self.option_count:
            self.highlighted = self.option_count - 1
        super().enter_list_mode()

    def append(self, entry: Entry) -> None:
        """Add ``entry``, dim while its agent thinks out loud in its turn, collapsed if its
        kind starts so; the view stays pinned to the latest entry unless the list is active or
        holds a cursor."""
        self._add(entry)

    def _add(self, entry: Entry) -> None:
        self._entries.append(entry)
        self._collapsed.append(starts_collapsed(entry.kind, entry.text))
        if self._shows(entry):
            self._show_after(len(self._entries) - 1)

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
        rules = [
            i for i, entry in enumerate(self._entries) if entry.agent == agent and ruled(entry.kind)
        ]
        if not rules:
            return
        index = rules[-1]
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
        self._add(Entry(text, kind, agent, call=call, speaker=speaker))

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
        if self.inspecting and call != entry.call:
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
        thinking = self._thinking(agent, turn)
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
        elif self._after_rows(promoted):
            for index in sorted(promoted):
                self._show_after(index)
        else:
            self._show_rows()

    def _after_rows(self, indexes: list[int]) -> bool:
        """Whether every entry at ``indexes`` comes after the last entry shown."""
        return not self._rows or all(index > self._rows[-1] for index in indexes)

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


class WorkingRow(Static):
    """What's running right now under the shown tab, one line per agent, below the
    logs; hidden while nothing shown runs. The :class:`Transcript` ticks its spinners and
    clocks while something runs."""

    DEFAULT_CSS = """
    WorkingRow {
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

    @property
    def running(self) -> bool:
        """Whether any agent runs."""
        return bool(self.working)

    def track(self, event: Event, roles: Mapping[str, str]) -> None:
        """Follow who runs after ``event``, with ``roles`` placing agents in tabs."""
        self.working = track_working(self.working, event, self.clock())
        self._roles = roles
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
        unwrapped = Text("\n", no_wrap=True, overflow="ellipsis")
        self.update(unwrapped.join(Text(line) for line in lines))
        self.set_class(bool(lines), "-running")


class DetailPane(ModalFocus, VerticalScroll):
    """The full content of the tool call or request under the conversation's cursor, below
    the logs and the working row, taking ``share`` percent of the conversation, or all of it
    while ``covering``; hidden until opened. Activated, j/k scroll it and escape leaves."""

    class Focused(Message):
        """The pane was activated."""

    DEFAULT_CSS = """
    DetailPane {
        display: none;
        border-top: solid $primary;
        border-title-align: left;
    }
    DetailPane.-open { display: block; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "scroll(1)", show=False),
        Binding("k", "scroll(-1)", show=False),
        Binding("escape", "normal_mode", show=False),
    ]

    share: reactive[int] = reactive(DETAIL_HEIGHT)
    covering: reactive[bool] = reactive(False)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._entry: int | None = None  # Where the entry shown is among its log's.
        self.call: ToolCall | None = None  # The call whose details are shown, if any.

    def compose(self) -> ComposeResult:
        yield Static()

    def watch_share(self) -> None:
        self._fit()

    def watch_covering(self) -> None:
        self._fit()

    def _fit(self) -> None:
        self.styles.height = "1fr" if self.covering else f"{self.share}%"

    @property
    def opened(self) -> bool:
        return self.has_class("-open")

    def show(self, call: ToolCall | None, entry: int | None) -> None:
        """Open, showing ``call``'s details, or a note that there are none, for the entry at
        ``entry``; another entry than the one shown starts at the top."""
        if not self.opened or entry != self._entry:
            self.scroll_home(animate=False)
        self._entry = entry
        self.call = call
        self.border_title = detail_title(call)
        self.query_one(Static).update(detail_body(call))
        self.add_class("-open")

    def close(self) -> None:
        """Close, leaving the pane if it's active."""
        if self.has_focus:
            self.screen.set_focus(None)
        self.remove_class("-open")

    def enter_scroll_mode(self) -> None:
        """Activate the pane, so j/k scroll it."""
        self._activate()

    def on_focus(self) -> None:
        self.post_message(self.Focused())

    def action_scroll(self, step: int) -> None:
        """Scroll ``step`` lines."""
        self.scroll_relative(y=step, animate=False)


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
    /* Kept displayed, at no height, so a log in list mode keeps its focus. */
    Transcript.-covered > TranscriptLog { height: 0; }
    Transcript.-covered > WorkingRow { display: none; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        # Within the conversation: from its list, or the pane itself, while the pane is open.
        Binding("J", "scroll_detail(1)", show=False),
        Binding("K", "scroll_detail(-1)", show=False),
        Binding("tab", "focus_detail", show=False),
        Binding("y", "copy_detail", show=False),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.only: str | None = None  # Show only this tab's entries, or all.
        self._label: str | None = None  # The shown tab's label; None for All.
        self.thinking_hidden = False
        self.clock: Callable[[], float] = time.monotonic
        self._logs: dict[str | None, TranscriptLog] = {}
        self._timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield self._new_log(None, shown=True)
        yield WorkingRow()
        yield DetailPane(id="detail")

    def on_mount(self) -> None:
        self._retitle()

    def on_resize(self) -> None:
        self._fit_detail()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # The conversation's own keys all work on the detail pane, so only while it's open.
        return self.detail.opened

    def action_scroll_detail(self, step: int) -> None:
        """Scroll the open detail pane ``step`` lines."""
        self.detail.action_scroll(step)

    def action_focus_detail(self) -> None:
        """Activate the open detail pane, so j/k scroll it."""
        self.detail.enter_scroll_mode()

    def action_copy_detail(self) -> None:
        """Copy the whole of the open detail pane's call to the clipboard, if it shows one,
        and say how many lines were copied."""
        text = detail_copy(self.detail.call)
        if text is None:
            return
        self.app.copy_to_clipboard(text)
        self.app.notify(copied_note(text))

    def close_detail(self) -> None:
        """Stop inspecting the shown log's entries, closing the detail pane."""
        self.shown.inspecting = False
        self.detail.close()
        self._fit_detail()

    def _fit_detail(self) -> None:
        """Have the open detail pane cover the whole conversation when it's short, or not."""
        covering = self.detail.opened and detail_covers(self.content_size.height)
        self.set_class(covering, "-covered")
        self.detail.covering = covering

    @property
    def shown(self) -> TranscriptLog:
        """The log of the tab being shown."""
        return self._logs[self.only]

    @property
    def working(self) -> WorkingRow:
        """The row of what's running, below the logs."""
        return self.query_one(WorkingRow)

    @property
    def detail(self) -> DetailPane:
        """The pane of the shown log's inspected entry, below the working row."""
        return self.query_one(DetailPane)

    @on(TranscriptLog.Inspect)
    def _inspect(self, event: TranscriptLog.Inspect) -> None:
        """Show the shown log's inspected entry's details, or close the pane."""
        event.stop()
        log = self.shown
        if log.inspecting:
            self.detail.show(log.detail, log.inspected)
        else:
            self.detail.close()
        self._fit_detail()

    def track(self, event: Event, roles: Mapping[str, str]) -> None:
        """Follow who runs after ``event``, with ``roles`` placing agents in tabs."""
        self.working.track(event, roles)
        self._keep_ticking()

    def entries(self, key: str | None) -> tuple[Entry, ...]:
        """The entries under the tab keyed ``key``; None is All."""
        return self._logs[key].entries if key in self._logs else ()

    def add_entry(self, entry: Entry, key: str | None = None) -> None:
        """Add ``entry`` under All and, if ``key`` names a tab, under that tab."""
        for tab in {None, key}:
            self._log(tab).append(entry)

    def replace_rule(self, text: str, kind: EntryKind, key: str | None, agent: str) -> None:
        """Make ``agent``'s latest rule read ``text`` as a ``kind`` entry, under All and, if
        ``key`` names a tab, under that tab."""
        for tab in {None, key}:
            self._log(tab).replace_rule(agent, text, kind)

    @property
    def ticking(self) -> bool:
        """Whether the spinners and clocks of running agents, tool calls and requests are
        turning."""
        return self._timer is not None

    def add_tool_call(
        self, event: ToolCallStarted | AgentRequest, key: str | None, speaker: str | None
    ) -> None:
        """Add the call or request to orbit ``event`` starts, spoken by ``speaker``, as a
        running entry under All and, if ``key`` names a tab, under that tab."""
        call = tool_call_started(event)
        for tab in {None, key}:
            self._log(tab).append_call(call, event.agent_id, speaker)
        self._keep_ticking()

    def finish_tool_call(self, event: ToolCallFinished | AgentReply) -> bool:
        """Show the call or request ``event`` finishes as finished in every log holding it;
        False when none does."""
        found = [log.finish_call(event) for log in self._logs.values()]
        self._keep_ticking()
        return any(found)

    def end_tool_calls(self, agent: str | None, requests: bool = False) -> None:
        """Show ``agent``'s tool calls still running as having no result, in every log, and
        its requests to orbit with no reply too if ``requests``."""
        for log in self._logs.values():
            log.end_calls(agent, requests)
        self._keep_ticking()

    def tick(self) -> None:
        """Turn the spinners of running tool calls, and those and the clocks of the working
        row."""
        for log in self._logs.values():
            log.tick()
        self.working.tick()

    @property
    def _anything_runs(self) -> bool:
        """Whether any agent or tool call runs."""
        return self.working.running or any(log.running for log in self._logs.values())

    def _keep_ticking(self) -> None:
        """Tick while any agent or tool call runs, and only then."""
        running = self._anything_runs
        if running and self._timer is None:
            self._timer = self.set_interval(1 / SPINNER_FPS, self.tick)
        elif not running and self._timer is not None:
            self._timer.stop()
            self._timer = None

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
        self.close_detail()
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
            self.mount(self._new_log(key), before=self.working)
        return self._logs[key]

    def _new_log(self, key: str | None, shown: bool = False) -> TranscriptLog:
        log = TranscriptLog(shown, self.thinking_hidden, lambda: self.clock())
        self._logs[key] = log
        return log
