"""The status line and modals that show how the link to a control plane in another process
stands."""

from collections.abc import Callable, Sequence
from typing import ClassVar, assert_never

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import OptionList, Static

from orbit.protocol import (
    Connected,
    Ended,
    HandshakeFailed,
    LinkState,
    Reconnecting,
    Stopped,
    TokenRefused,
)
from orbit.tui.view.link import (
    CHOICES,
    QUIT,
    STOPPED_TITLE,
    final_notice,
    lost_delay,
    lost_message,
    lost_title,
    reconnecting_status,
    stopped_message,
)


class LinkScreen(ModalScreen[int]):
    """``title`` with ``message`` and the ``choices`` to pick from, the first preselected;
    dismisses with the index of the one picked, or the first's on escape."""

    AUTO_FOCUS = "OptionList"
    DEFAULT_CSS = """
    LinkScreen { align: center middle; }
    LinkScreen > Vertical {
        width: 70;
        max-width: 90%;
        height: auto;
        border: round $error;
        padding: 0 1;
    }
    LinkScreen OptionList { height: auto; border: none; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "pick_first", show=False)]

    def __init__(self, title: str, message: str, choices: Sequence[str]) -> None:
        super().__init__()
        self.title_text = title
        self.message = message
        self.choices = tuple(choices)

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = self.title_text
            yield Static(Text(self.message), id="link-message")
            yield OptionList(*self.choices)

    def on_mount(self) -> None:
        self.query_one(OptionList).highlighted = 0

    @on(OptionList.OptionSelected)
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_index)

    def action_pick_first(self) -> None:
        self.dismiss(0)


_SOONEST = 0.01  # Textual's timers can't wait no time at all.

Push = Callable[[LinkScreen, Callable[[int | None], None]], object]
ShowStatus = Callable[[str | None], None]
After = Callable[[float, Callable[[], None]], Timer]


class LinkHost:
    """Follows the link to the control plane: while it's down, the status line is shown with
    ``show_status`` and, after two minutes, a modal pushed with ``push`` asks whether to keep
    trying, as one does at once when the control plane stops, and a link that won't be retried
    is shown with only the choice to quit; ``quit`` ends the app, and
    ``after`` calls back after a delay."""

    def __init__(self, push: Push, quit: Callable[[], None], show_status: ShowStatus, after: After):
        self._push = push
        self._quit = quit
        self._show_status = show_status
        self._after = after
        self._timer: Timer | None = None  # Until the user is asked whether to keep trying.
        self.shown: LinkScreen | None = None  # The modal asking, if open.

    def follow(self, state: LinkState, now: float) -> None:
        """Show ``state``, the link's, as of ``now``, a ``time.monotonic()`` reading."""
        match state:
            case Connected():
                self._connected()
            case Reconnecting(url=url, since=since):
                self._reconnecting(url, lost_delay(since, now))
            case Stopped(url=url):
                self._ask(STOPPED_TITLE, stopped_message(url))
            case TokenRefused() | HandshakeFailed() | Ended():
                self._final(*final_notice(state))
            case _:
                assert_never(state)

    def _connected(self) -> None:
        """Hide the status line and stop waiting to ask, closing the modal if it's open."""
        self._show_status(None)
        self._stop_timer()
        if self.shown is not None:
            self.shown.dismiss(0)
            self.shown = None  # Its close callback runs later, after any stop that follows.

    def _reconnecting(self, url: str, delay: float) -> None:
        """Show that the link to ``url`` is being retried, asking whether to keep trying in
        ``delay`` seconds unless that's already due."""
        self._show_status(reconnecting_status(url))
        if self._timer is None:
            ask = lambda: self._ask(lost_title(url), lost_message(url))  # noqa: E731
            self._timer = self._after(max(delay, _SOONEST), ask)

    def _final(self, title: str, message: str) -> None:
        """Show ``title`` with ``message``, saying why the link won't be retried, quitting once
        it's closed."""
        self._show_status(None)
        self._stop_timer()
        self._push(LinkScreen(title, message, (QUIT,)), lambda _: self._quit())

    def _ask(self, title: str, message: str) -> None:
        """Ask ``title`` with ``message`` whether to keep trying, unless that's being asked."""
        if self.shown is not None:
            return
        screen = self.shown = LinkScreen(title, message, CHOICES)

        def answered(index: int | None) -> None:
            if self.shown is screen:  # Not one shown since.
                self.shown = None
            if index is not None and CHOICES[index] == QUIT:
                self._quit()

        self._push(screen, answered)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
