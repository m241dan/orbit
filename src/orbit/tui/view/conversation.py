"""The conversation window, without the widgets showing it: its tabs' logs and the one shown,
the working row and the detail pane on the shown log's cursor, and its title."""

from collections.abc import Callable, Iterable, Mapping
from typing import Protocol

from orbit.protocol import AgentReply, AgentRequest, Event, ToolCallFinished, ToolCallStarted
from orbit.tui.view.common import EntryKind
from orbit.tui.view.detail import copied_note, detail_copy, detail_covers
from orbit.tui.view.detail_views import Detail
from orbit.tui.view.log import Entry, EntryLog
from orbit.tui.view.tool_calls import tool_call_started
from orbit.tui.view.transcript import conversation_title
from orbit.tui.view.working import WorkingLines


class LogPane(Protocol):
    """A tab's log as the conversation shows it: its entries, and the pane they're shown in."""

    @property
    def entry_log(self) -> EntryLog:
        """The log's entries."""
        ...

    @property
    def has_focus(self) -> bool:
        """Whether the log is active."""
        ...

    def show(self, shown: bool) -> None:
        """Display the log if ``shown``, or hide it."""
        ...

    def show_place(self) -> None:
        """Scroll to the cursor's entry, or to the latest entry if there's no cursor."""
        ...

    def remove(self) -> object:
        """Take the log out of the conversation."""
        ...


class ConversationView[P: LogPane](Protocol):
    """The conversation window, as the conversation is shown in it, with its logs, each a
    ``P``."""

    @property
    def content_height(self) -> int:
        """How many rows the window holds."""
        ...

    def new_log(self, thinking_hidden: bool) -> P:
        """A log added to the window, hidden, leaving out thinking if ``thinking_hidden``."""
        ...

    def show_title(self, title: str) -> None:
        """Title the window ``title``."""
        ...

    def cover(self, covering: bool) -> None:
        """Have the open detail pane cover the whole window if ``covering``, or not."""
        ...

    def keep_ticking(self, running: bool) -> None:
        """Tick the conversation while ``running``, and only then."""
        ...

    def unfocus(self) -> None:
        """Leave whatever is active."""
        ...

    def call_after_refresh(self, callback: Callable[[], object]) -> object:
        """Call ``callback`` once the window is laid out again."""
        ...

    def copy(self, text: str, note: str) -> None:
        """Copy ``text`` to the clipboard, saying so in ``note``."""
        ...


class Conversation[P: LogPane]:
    """The running conversation, shown in ``view``: its entries are kept per tab (All, and the
    tab they belong to), each tab in a log of its own, starting with All's ``first``, one of
    which is shown. ``working`` follows who runs, and ``detail`` shows the shown log's
    inspected entry."""

    def __init__(
        self, view: ConversationView[P], first: P, working: WorkingLines, detail: Detail
    ) -> None:
        self._view = view
        self._working = working
        self._detail = detail
        self.only: str | None = None  # Show only this tab's entries, or all.
        self._label: str | None = None  # The shown tab's label; None for All.
        self._session: str | None = None  # The open session's name, once one is known.
        self.thinking_hidden = False
        self._logs: dict[str | None, P] = {None: first}

    @property
    def shown(self) -> P:
        """The log of the tab being shown."""
        return self._logs[self.only]

    def retitle(self) -> None:
        """Title the window by the tab shown, whether thinking is hidden and the session."""
        self._view.show_title(
            conversation_title(self._label, self.thinking_hidden, session=self._session)
        )

    def copy_detail(self) -> None:
        """Copy the whole of the open detail pane's call to the clipboard, if it shows one,
        saying how many lines were copied."""
        text = detail_copy(self._detail.call)
        if text is not None:
            self._view.copy(text, copied_note(text))

    def close_detail(self) -> None:
        """Stop inspecting the shown log's entries, closing the detail pane."""
        self.shown.entry_log.inspecting = False
        self._detail.close()
        self.fit_detail()

    def fit_detail(self) -> None:
        """Have the open detail pane cover the whole conversation when it's short, or not."""
        self._view.cover(self._detail.opened and detail_covers(self._view.content_height))

    def inspect(self) -> None:
        """Show the shown log's inspected entry's details, or close the pane."""
        log = self.shown.entry_log
        if log.inspecting:
            self._detail.show(log.detail)
        else:
            self._detail.close()
        self.fit_detail()

    def track(self, event: Event, roles: Mapping[str, str]) -> None:
        """Follow who runs after ``event``, with ``roles`` placing agents in tabs."""
        self._working.track(event, roles)
        self._keep_ticking()

    def entries(self, key: str | None) -> tuple[Entry, ...]:
        """The entries under the tab keyed ``key``; None is All."""
        return self._logs[key].entry_log.entries if key in self._logs else ()

    def add_entry(self, entry: Entry, key: str | None = None) -> None:
        """Add ``entry`` under All and, if ``key`` names a tab, under that tab."""
        for tab in {None, key}:
            self._log(tab).append(entry)

    def drop_entry(self, entry: Entry) -> None:
        """Drop ``entry`` from every log holding it."""
        for log in self._entry_logs:
            log.drop(entry)

    def swap_entry(self, entry: Entry, new: Entry) -> None:
        """Put ``new`` in place of ``entry`` in every log holding it."""
        for log in self._entry_logs:
            log.swap(entry, new)

    def clear(self) -> None:
        """Drop every entry, under All and every tab, closing the detail pane."""
        self.close_detail()
        shown = self.shown
        for key, log in list(self._logs.items()):
            if log is shown:
                log.entry_log.clear()
            else:
                del self._logs[key]
                log.remove()
        self._keep_ticking()

    def prepend(self, add: Callable[[], None]) -> None:
        """Have the entries ``add`` adds come before every entry, in every log, each log's
        cursor kept on its entry; their calls still running show as having no result, since
        their results, if any, are already shown."""
        logs = self._entry_logs
        for log in logs:
            log.begin_earlier()
        add()
        for log in self._entry_logs:
            # A log ``add`` opened holds only earlier entries.
            if log in logs:
                log.end_earlier()
            else:
                log.end_running_calls()
        self._keep_ticking()

    def replace_rule(self, text: str, kind: EntryKind, key: str | None, agent: str) -> None:
        """Make ``agent``'s latest rule read ``text`` as a ``kind`` entry, under All and, if
        ``key`` names a tab, under that tab."""
        for tab in {None, key}:
            self._log(tab).replace_rule(agent, text, kind)

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
        found = [log.finish_call(event) for log in self._entry_logs]
        self._keep_ticking()
        return any(found)

    def end_tool_calls(self, agent: str | None, requests: bool = False) -> None:
        """Show ``agent``'s tool calls still running as having no result, in every log, and
        its requests to orbit with no reply too if ``requests``."""
        for log in self._entry_logs:
            log.end_calls(agent, requests)
        self._keep_ticking()

    def tick(self) -> None:
        """Turn the spinners of running tool calls, and those and the clocks of the working
        row."""
        for log in self._entry_logs:
            log.tick()
        self._working.tick()

    def _keep_ticking(self) -> None:
        """Tick while any agent or tool call runs, and only then."""
        running = self._working.running or any(log.running for log in self._entry_logs)
        self._view.keep_ticking(running)

    def promote(self, agent: str | None, turn: str, places: Iterable[int]) -> None:
        """Draw at full brightness, in every log holding them, the messages ``agent`` thought
        out loud in ``turn`` at ``places`` among them."""
        shown = tuple(places)
        for log in self._entry_logs:
            log.promote(agent, turn, shown)

    def toggle_thinking(self) -> None:
        """Hide agents' dim messages in every tab, or show them again."""
        self.thinking_hidden = not self.thinking_hidden
        for log in self._entry_logs:
            log.hide_thinking(self.thinking_hidden)
        self.retitle()

    def release_cursor(self) -> None:
        """Let go of the shown log's cursor, so it follows the latest entry again, closing the
        detail pane that showed its entry."""
        self.close_detail()
        self.shown.entry_log.release_cursor()

    def show_only(self, key: str | None, label: str = "") -> None:
        """Show only the tab keyed ``key``'s entries, titled by its tab ``label``, or all."""
        if self.shown.has_focus:
            self._view.unfocus()
        self.close_detail()
        before = self.shown
        self.only = key
        self._label = None if key is None else label
        self.retitle()
        self._working.show_tab(key)
        log = self._logs[key] if key in self._logs else self._new_log(key)
        if log is not before:
            before.show(False)
            log.show(True)
        # Laid out once shown, which is when its end is known.
        self._view.call_after_refresh(log.show_place)

    def relabel(self, label: str) -> None:
        """Title the shown tab by its new ``label``, leaving what's shown as it is."""
        self._label = label
        self.retitle()

    def show_session(self, name: str | None) -> None:
        """End the title with the open session's ``name``, or with none."""
        self._session = name
        self.retitle()

    @property
    def _entry_logs(self) -> list[EntryLog]:
        """Every log's entries."""
        return [log.entry_log for log in self._logs.values()]

    def _log(self, key: str | None) -> EntryLog:
        """The entries of the tab keyed ``key``, opening its log if it has none yet."""
        log = self._logs[key] if key in self._logs else self._new_log(key)
        return log.entry_log

    def _new_log(self, key: str | None) -> P:
        log = self._view.new_log(self.thinking_hidden)
        self._logs[key] = log
        return log
