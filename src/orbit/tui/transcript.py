"""The conversation window: the transcript, coordinating its per-tab logs, the working row
and the detail pane."""

import time
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.widgets import Static

from orbit.protocol import Event
from orbit.tui.detail import DetailPane
from orbit.tui.transcript_log import TranscriptLog
from orbit.tui.view.conversation import Conversation
from orbit.tui.view.history import LOADING_EARLIER, TRIMMED_EARLIER
from orbit.tui.view.log import Entry
from orbit.tui.widgets import Ticker
from orbit.tui.working import WorkingRow


class Transcript(Vertical):
    """Main display: shows the :class:`Conversation`, pinned to the latest output unless the
    shown tab's log holds a cursor. Each tab's entries are in a :class:`TranscriptLog` of
    their own, one of which is shown, above the working row and the detail pane."""

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
    Transcript > #loading-earlier { display: none; color: $text-muted; }
    Transcript > #loading-earlier.-loading { display: block; }
    Transcript > #trimmed-earlier { display: none; color: $text-muted; }
    Transcript > #trimmed-earlier.-trimmed { display: block; }
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
        self.clock: Callable[[], float] = time.monotonic
        self._working = WorkingRow()
        self._detail = DetailPane(id="detail")
        self._all = self._log(shown=True, thinking_hidden=False)
        self.conversation = Conversation(
            self, self._all, self._working.working, self._detail.detail
        )
        self._ticker = Ticker(self, self.tick)

    def compose(self) -> ComposeResult:
        yield Static(TRIMMED_EARLIER, id="trimmed-earlier")
        yield Static(LOADING_EARLIER, id="loading-earlier")
        yield self._all
        yield self._working
        yield self._detail

    def on_mount(self) -> None:
        self.conversation.retitle()

    def on_resize(self) -> None:
        self.conversation.fit_detail()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # The conversation's own keys all work on the detail pane, so only while it's open.
        return self._detail.opened

    def action_scroll_detail(self, step: int) -> None:
        """Scroll the open detail pane ``step`` lines."""
        self._detail.action_scroll(step)

    def action_focus_detail(self) -> None:
        """Activate the open detail pane, so j/k scroll it."""
        self._detail.enter_scroll_mode()

    def action_copy_detail(self) -> None:
        """Copy the whole of the open detail pane's call to the clipboard, if it shows one."""
        self.conversation.copy_detail()

    @property
    def only(self) -> str | None:
        """The key of the tab whose entries alone are shown; None for All."""
        return self.conversation.only

    @property
    def thinking_hidden(self) -> bool:
        """Whether agents' dim messages are left out."""
        return self.conversation.thinking_hidden

    @property
    def shown(self) -> TranscriptLog:
        """The log of the tab being shown."""
        return self.conversation.shown

    @property
    def detail(self) -> DetailPane:
        """The pane of the shown log's inspected entry, below the working row."""
        return self._detail

    @on(TranscriptLog.Inspect)
    def _inspect(self, event: TranscriptLog.Inspect) -> None:
        event.stop()
        self.conversation.inspect()

    def track(self, event: Event, roles: Mapping[str, str]) -> None:
        """Follow who runs after ``event``, with ``roles`` placing agents in tabs."""
        self.conversation.track(event, roles)

    def entries(self, key: str | None) -> tuple[Entry, ...]:
        """The entries under the tab keyed ``key``; None is All."""
        return self.conversation.entries(key)

    def show_loading(self, loading: bool) -> None:
        """Show, above the entries, that earlier ones are ``loading``, or stop."""
        self.query_one("#loading-earlier").set_class(loading, "-loading")

    @property
    def loading_earlier(self) -> bool:
        """Whether the conversation shows that earlier entries are loading."""
        return self.query_one("#loading-earlier").has_class("-loading")

    def show_trimmed(self, trimmed: bool) -> None:
        """Show, above the entries, that earlier ones were ``trimmed`` from the log, or stop."""
        self.query_one("#trimmed-earlier").set_class(trimmed, "-trimmed")

    @property
    def trimmed_earlier(self) -> bool:
        """Whether the conversation shows that earlier entries were trimmed from the log."""
        return self.query_one("#trimmed-earlier").has_class("-trimmed")

    @property
    def ticking(self) -> bool:
        """Whether the spinners and clocks of running agents, tool calls and requests are
        turning."""
        return self._ticker.ticking

    def tick(self) -> None:
        """Turn the spinners of running tool calls, and those and the clocks of the working
        row."""
        self.conversation.tick()

    def enter_list_mode(self) -> None:
        """Activate the shown log: j/k move between its entries, enter collapses or expands
        one, and escape leaves."""
        self.shown.enter_list_mode()

    def release_cursor(self) -> None:
        """Let go of the shown log's cursor, so it follows the latest entry again, closing the
        detail pane that showed its entry."""
        self.conversation.release_cursor()

    def show_only(self, key: str | None, label: str = "") -> None:
        """Show only the tab keyed ``key``'s entries, titled by its tab ``label``, or all."""
        self.conversation.show_only(key, label)

    @property
    def content_height(self) -> int:
        return self.content_size.height

    def new_log(self, thinking_hidden: bool) -> TranscriptLog:
        log = self._log(shown=False, thinking_hidden=thinking_hidden)
        self.mount(log, before=self._working)
        return log

    def _log(self, shown: bool, thinking_hidden: bool) -> TranscriptLog:
        # Read through, so a clock set later still turns the spinners.
        return TranscriptLog(shown, thinking_hidden, lambda: self.clock())

    def show_title(self, title: str) -> None:
        self.border_title = title

    def show_corner(self, text: str) -> None:
        self.border_subtitle = text

    def cover(self, covering: bool) -> None:
        self.set_class(covering, "-covered")
        self._detail.covering = covering

    def keep_ticking(self, running: bool) -> None:
        self._ticker.keep(running)

    def unfocus(self) -> None:
        self.screen.set_focus(None)

    def copy(self, text: str, note: str) -> None:
        self.app.copy_to_clipboard(text)
        self.app.notify(note)
