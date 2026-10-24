"""A tab's log of transcript entries: which of them are shown, and where, how each is drawn,
and the log itself, without the list showing it."""

from bisect import bisect_left
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from orbit.protocol import AgentReply, ToolCallFinished
from orbit.tui.view.common import EntryKind
from orbit.tui.view.tool_calls import (
    ToolCall,
    finishes,
    tool_call_finished,
    tool_call_unfinished,
)
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


def shown(entry: Entry, thinking_hidden: bool) -> bool:
    """Whether ``entry`` is shown: all are, but an agent's thinking out loud when
    ``thinking_hidden``."""
    return not (thinking_hidden and entry.turn is not None)


def shown_rows(entries: Sequence[Entry], thinking_hidden: bool) -> list[int]:
    """Where the ``entries`` shown are among them, in order: the entry each option shows."""
    return [i for i, entry in enumerate(entries) if shown(entry, thinking_hidden)]


def nearest_row(rows: Sequence[int], index: int) -> int | None:
    """The option among ``rows`` of the entry at ``index``, or of the nearest shown entry
    before it, else after it; None when none is shown."""
    before = [row for row, entry in enumerate(rows) if entry <= index]
    if before:
        return before[-1]
    return 0 if rows else None


def after_rows(rows: Sequence[int], indexes: Sequence[int]) -> bool:
    """Whether every entry at ``indexes`` comes after the last entry ``rows`` shows."""
    return not rows or all(index > rows[-1] for index in indexes)


def thinking_places(entries: Sequence[Entry], agent: str | None, turn: str) -> list[int]:
    """Where among ``entries`` the messages ``agent`` is thinking out loud in ``turn`` are, in
    order."""
    return [i for i, entry in enumerate(entries) if (entry.agent, entry.turn) == (agent, turn)]


def latest_rule(entries: Sequence[Entry], agent: str) -> int | None:
    """Where among ``entries`` ``agent``'s latest rule is; None when it has none."""
    rules = [i for i, entry in enumerate(entries) if entry.agent == agent and ruled(entry.kind)]
    return rules[-1] if rules else None


def entry_action(entry: Entry) -> Literal["inspect", "toggle"]:
    """What choosing ``entry`` does: a tool call or request is inspected, anything else is
    collapsed or expanded."""
    return "toggle" if entry.call is None else "inspect"


def entry_component(entry: Entry) -> str:
    """The component class ``entry`` is styled by: an agent's thinking out loud is styled as
    thinking, anything else as its kind."""
    component = "thinking" if entry.turn is not None else entry.kind
    return f"transcript--{component}"


@dataclass(frozen=True)
class EntryRow:
    """An entry as its option draws it: the ``gutter`` in front of every row, styled by the
    ``gutter_component`` class; the ``heading`` label row, if it has one; then its ``body``,
    a rule or markdown of its ``text`` as an entry of ``kind`` shows it, or else its
    ``label``, styled by the ``component`` class, on a band of that style if ``banded``."""

    text: str
    kind: EntryKind
    label: str
    component: str
    gutter: str
    gutter_component: str
    heading: str | None = None
    body: Literal["rule", "markdown"] | None = None
    banded: bool = False


def entry_row(entry: Entry, collapsed: bool, marked: bool, previous: str | None) -> EntryRow:
    """How ``entry`` is drawn, ``collapsed`` or not and ``marked`` as the cursor's or not,
    after an entry spoken by ``previous`` (None for none, or the planner)."""
    return EntryRow(
        text=entry.text,
        kind=entry.kind,
        label=entry_label(entry.kind, entry.text, collapsed),
        component=entry_component(entry),
        gutter=gutter(marked),
        gutter_component="transcript--marker" if marked else "transcript--gutter",
        heading=speaker_label(entry.kind, entry.speaker, previous),
        body=_body(entry.kind, collapsed),
        banded=banded(entry.kind),
    )


def _body(kind: EntryKind, collapsed: bool) -> Literal["rule", "markdown"] | None:
    """What an entry of ``kind`` draws instead of its label: a rule, or markdown, if it
    does."""
    if ruled(kind):
        return "rule"
    if shows_markdown(kind, collapsed):
        return "markdown"
    return None


type _SetAside = tuple[list[Entry], list[bool], dict[int, ToolCall], int | None]
"""A log's entries, whether each is collapsed, its running calls and its cursor's entry."""


class EntryLogView(Protocol):
    """A tab's log, as its entries are shown in it: a list of options, one per entry shown,
    whose cursor the user moves."""

    @property
    def highlighted(self) -> int | None:
        """The option the cursor is on, if any."""
        ...

    @property
    def has_focus(self) -> bool:
        """Whether the log is active."""
        ...

    def highlight(self, row: int | None) -> None:
        """Put the cursor on option ``row``, or let go of it for none."""
        ...

    def add_row(self, row: EntryRow) -> None:
        """Add ``row`` as the last option."""
        ...

    def redraw(self, index: int, row: EntryRow) -> None:
        """Draw option ``index`` as ``row``."""
        ...

    def replace_rows(self, rows: Sequence[EntryRow], highlight: int | None) -> None:
        """Show ``rows`` in place of the options, the cursor on option ``highlight``, if any,
        scrolling to the cursor's place, or the end, once they're laid out."""
        ...

    def clear_rows(self) -> None:
        """Drop every option."""
        ...

    def scroll_to_end(self) -> None:
        """Scroll to the latest entry."""
        ...

    def detail_changed(self) -> None:
        """Say that what the detail pane should show may have changed: whether the log is
        inspecting, or its detail."""
        ...


class EntryLog:
    """One tab's entries, shown in ``view`` an option each: enter collapses or expands one,
    and the cursor's entry is marked in the left gutter. Hiding thinking leaves agents' dim
    messages out of the options; the entries are all kept, and option indexes count only the
    entries shown. A tool call is one line, updated in place as it finishes, its spinner read
    off ``clock``. Inspecting, enter on a tool call or request asks for its details, which
    follow the cursor until escape. The entries added between :meth:`begin_earlier` and
    :meth:`end_earlier` go above the rest."""

    def __init__(
        self, view: EntryLogView, thinking_hidden: bool, clock: Callable[[], float]
    ) -> None:
        self._view = view
        self._clock = clock
        self._entries: list[Entry] = []
        self._collapsed: list[bool] = []
        self._rows: list[int] = []  # The entry each option shows.
        self._marked: int | None = None  # The option drawn with the marker.
        self._calls: dict[int, ToolCall] = {}  # The calls still running, by their entry.
        self._later: _SetAside | None = None  # Set aside while earlier entries are added.
        self._thinking_hidden = thinking_hidden
        self.inspecting = False  # Whether the detail pane shows the cursor's entry.

    @property
    def entries(self) -> tuple[Entry, ...]:
        return tuple(self._entries)

    @property
    def inspected(self) -> int | None:
        """Where the cursor's entry is among the entries, if there's a cursor."""
        highlighted = self._view.highlighted
        return None if highlighted is None else self._rows[highlighted]

    @property
    def detail(self) -> ToolCall | None:
        """The tool call or request of the cursor's entry, if it's one."""
        index = self.inspected
        return None if index is None else self._entries[index].call

    def inspect(self, inspecting: bool) -> None:
        """Show the cursor's entry's details as it moves if ``inspecting``, or stop."""
        self.inspecting = inspecting
        self._view.detail_changed()

    def stop_inspecting(self) -> bool:
        """Stop inspecting, saying whether the log was."""
        if not self.inspecting:
            return False
        self.inspect(False)
        return True

    def blurred(self, to_detail: bool) -> None:
        """Stop inspecting, now the log is no longer active, unless the focus went
        ``to_detail``, the detail pane: the pane only follows a log in list mode."""
        if self.inspecting and not to_detail:
            self.inspect(False)

    def at_top(self) -> bool:
        """Whether the cursor is on the first entry, where moving up asks for earlier ones
        instead."""
        return self._view.highlighted == 0

    def start_at_latest(self) -> None:
        """Put the cursor on the latest entry when the log holds none, as it's activated."""
        if self._view.highlighted is None and self._rows:
            self._view.highlight(len(self._rows) - 1)

    def append(self, entry: Entry) -> None:
        """Add ``entry``, dim while its agent thinks out loud in its turn, collapsed if its
        kind starts so; the view stays pinned to the latest entry unless the log is active or
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
        self._view.clear_rows()

    def begin_earlier(self) -> None:
        """Set the entries aside, so those added until :meth:`end_earlier` come before them."""
        self._later = (self._entries, self._collapsed, self._calls, self.inspected)
        self._entries, self._collapsed, self._calls = [], [], {}
        self._rows, self._marked = [], None
        self._view.clear_rows()

    def end_earlier(self) -> None:
        """Put the entries set aside back after those added since, whose calls still running
        are shown as having no result, the cursor kept on the entry it was on."""
        assert self._later is not None  # Only ever after begin_earlier.
        entries, collapsed, calls, held = self._later
        self._later = None
        self.end_running_calls()
        offset = len(self._entries)
        self._entries += entries
        self._collapsed += collapsed
        self._calls = {index + offset: call for index, call in calls.items()}
        self._show_rows_holding(None if held is None else held + offset)

    def drop(self, entry: Entry) -> None:
        """Drop ``entry``, this very one rather than an equal one, the cursor kept on its own
        entry, or moved to the nearest one shown if it held ``entry``; a log without it is left
        as it is."""
        index = next((i for i, held in enumerate(self._entries) if held is entry), None)
        if index is None:
            return
        cursor = self.inspected
        del self._entries[index]
        del self._collapsed[index]
        self._calls = {i - (i > index): call for i, call in self._calls.items()}
        self._show_rows_holding(None if cursor is None else cursor - (cursor > index))

    def _show_after(self, index: int) -> None:
        """Add the entry at ``index``, which comes after every entry shown, as the last option,
        the view pinned to it unless the log is active or holds a cursor."""
        self._rows.append(index)
        self._view.add_row(self._drawn(len(self._rows) - 1))
        if not self._view.has_focus and self._view.highlighted is None:
            self._view.scroll_to_end()

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
        text, kind = tool_call_entry(call, agent, self._clock())
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

    def end_running_calls(self) -> None:
        """Show every call and request still running as having no result."""
        for index, call in list(self._calls.items()):
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
        text, kind = tool_call_entry(call, entry.agent, self._clock())
        self._entries[index] = replace(entry, text=text, kind=kind, call=call)
        if self.inspecting and call != entry.call and self.inspected == index:
            self._view.detail_changed()
        if (text, kind) != (entry.text, entry.kind):
            self._redraw(bisect_left(self._rows, index))  # Tool calls are never hidden.

    def release_cursor(self) -> None:
        """Let go of the held cursor, so the view follows the latest entry again."""
        self._view.highlight(None)
        self._view.scroll_to_end()

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
        self._show_rows_holding(self.inspected)

    def _show_rows_holding(self, held: int | None) -> None:
        """Rebuild the options from the entries shown, the cursor on the entry at ``held``, or
        the nearest one shown, if given."""
        self._rows = shown_rows(self._entries, self._thinking_hidden)
        self._marked = None  # Its option is about to go.
        rows = [self._drawn(row) for row in range(len(self._rows))]
        self._view.replace_rows(rows, None if held is None else nearest_row(self._rows, held))

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

    def moved(self, highlighted: int | None) -> None:
        """Move the marker from the entry it was on to the cursor's, now on option
        ``highlighted``, if there is one."""
        previous, self._marked = self._marked, highlighted
        for index in (previous, highlighted):
            if index is not None:
                self._redraw(index)
        if self.inspecting:
            self._view.detail_changed()

    def choose(self, row: int) -> None:
        """Inspect the tool call or request at option ``row``; collapse or expand any other
        entry."""
        if entry_action(self._entries[self._rows[row]]) == "toggle":
            self.toggle(row)
        else:
            self.inspect(True)

    def _redraw(self, row: int) -> None:
        self._view.redraw(row, self._drawn(row))

    def _drawn(self, row: int) -> EntryRow:
        """How the entry at option ``row`` is drawn."""
        index = self._rows[row]
        entry = self._entries[index]
        previous = self._entries[self._rows[row - 1]].speaker if row else None
        return entry_row(entry, self._collapsed[index], row == self._marked, previous)
