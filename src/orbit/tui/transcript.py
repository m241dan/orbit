"""The conversation window: the transcript, coordinating its per-tab logs, the working row
and the detail pane."""

import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any, ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical

from orbit.protocol import AgentReply, AgentRequest, Event, ToolCallFinished, ToolCallStarted
from orbit.tui.detail import DetailPane
from orbit.tui.transcript_log import TranscriptLog
from orbit.tui.view.common import EntryKind
from orbit.tui.view.detail import copied_note, detail_copy, detail_covers
from orbit.tui.view.log import Entry
from orbit.tui.view.tool_calls import tool_call_started
from orbit.tui.view.transcript import conversation_title
from orbit.tui.widgets import Ticker
from orbit.tui.working import WorkingRow


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
        self._session: str | None = None  # The open session's name, once one is known.
        self.thinking_hidden = False
        self.clock: Callable[[], float] = time.monotonic
        self._logs: dict[str | None, TranscriptLog] = {}
        self._ticker = Ticker(self, self.tick)

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
            self.detail.show(log.detail)
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
        return self._ticker.ticking

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
        self._ticker.keep(self._anything_runs)

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
        """Let go of the shown log's cursor, so it follows the latest entry again, closing the
        detail pane that showed its entry."""
        self.close_detail()
        self.shown.release_cursor()

    def show_only(self, key: str | None, label: str = "") -> None:
        """Show only the tab keyed ``key``'s entries, titled by its tab ``label``, or all."""
        if self.shown.has_focus:
            self.screen.set_focus(None)
        self.close_detail()
        before = self.shown
        self.only = key
        self._label = None if key is None else label
        self._retitle()
        self.working.show_tab(key)
        log = self._log(key)
        if log is not before:
            before.show(False)
            log.show(True)
        # Laid out once shown, which is when its end is known.
        self.call_after_refresh(log.show_place)

    def show_session(self, name: str | None) -> None:
        """End the title with the open session's ``name``, or with none."""
        self._session = name
        self._retitle()

    def _retitle(self) -> None:
        self.border_title = conversation_title(
            self._label, self.thinking_hidden, session=self._session
        )

    def _log(self, key: str | None) -> TranscriptLog:
        if key not in self._logs:
            self.mount(self._new_log(key), before=self.working)
        return self._logs[key]

    def _new_log(self, key: str | None, shown: bool = False) -> TranscriptLog:
        log = TranscriptLog(shown, self.thinking_hidden, lambda: self.clock())
        self._logs[key] = log
        return log
