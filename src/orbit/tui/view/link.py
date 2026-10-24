"""How the link to a control plane in another process is shown."""

from collections.abc import Awaitable, Callable
from typing import Protocol, assert_never

from orbit.protocol import (
    Connected,
    Ended,
    HandshakeFailed,
    LinkState,
    Reconnecting,
    Stopped,
    TokenRefused,
)

LOST_AFTER = 120.0
"""How many seconds of reconnecting go by before the user is asked whether to keep trying."""

KEEP_TRYING = "Keep trying"
QUIT = "Quit"
CHOICES = (KEEP_TRYING, QUIT)
"""The choices a lost or stopped control plane offers, the first preselected."""

NUMBER_WORDS = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
)

STOPPED_TITLE = "The control plane stopped"
REFUSED_TITLE = "Can't connect"
ENDED_TITLE = "Disconnected"


def reconnecting_status(url: str) -> str:
    """The status line shown by the prompt while reconnecting to ``url``."""
    return f"Reconnecting to {url}…"


def lost_title(url: str) -> str:
    """The title of the modal asking whether to keep trying ``url``."""
    return f"Lost the control plane at {url}"


def lost_message(url: str) -> str:
    """What the modal asking whether to keep trying ``url`` says."""
    return f"Orbit has been trying to reconnect to {url} for {minutes_text(LOST_AFTER)}."


def minutes_text(seconds: float) -> str:
    """The whole minutes in ``seconds`` in words: ``one minute``, ``two minutes``, or ``12
    minutes`` past ten."""
    minutes = int(seconds // 60)
    count = NUMBER_WORDS[minutes] if minutes < len(NUMBER_WORDS) else str(minutes)
    return f"{count} minute{'s' * (minutes != 1)}"


def stopped_message(url: str) -> str:
    """What the modal saying the control plane at ``url`` stopped says."""
    return f"The control plane at {url} is shutting down. Orbit keeps trying to reconnect."


def ended_message(url: str, reason: str) -> str:
    """What the modal saying the connection to the control plane at ``url`` ended for good, for
    ``reason`` if one was given, says."""
    ended = f"The connection to the control plane at {url} ended"
    because = f": {reason}." if reason else "."
    return f"{ended}{because} Reconnecting would end the same way."


def final_notice(state: TokenRefused | HandshakeFailed | Ended) -> tuple[str, str]:
    """The title and message of the modal saying why the link, in ``state``, isn't retried."""
    if isinstance(state, TokenRefused | HandshakeFailed):
        return REFUSED_TITLE, state.message
    return ENDED_TITLE, ended_message(state.url, state.reason)


def lost_delay(since: float, now: float) -> float:
    """How many seconds from ``now`` until the user is asked whether to keep trying, for a link
    down ``since``."""
    return max(0.0, LOST_AFTER - (now - since))


_SOONEST = 0.01  # Textual's timers can't wait no time at all.

Closed = Callable[[int | None], Awaitable[None]]
"""Takes the index of the choice a modal was closed with, if any."""


class Timer(Protocol):
    """A call to make after a delay, unless stopped first."""

    def stop(self) -> object:
        """Stop the call being made, if it's still to be."""
        ...


After = Callable[[float, Callable[[], None]], Timer]
"""Calls back after a delay, in seconds, unless the timer it returns is stopped first."""


class LinkView(Protocol):
    """Where the link shows: the status line by the prompt, and its modal, one at a time,
    which the user closes by picking one of its choices."""

    @property
    def modal_open(self) -> bool:
        """Whether the link's modal is open."""
        ...

    def show_status(self, status: str | None) -> None:
        """Show ``status`` by the prompt, or hide that line for none."""
        ...

    def open_modal(
        self, title: str, message: str, choices: tuple[str, ...], closed: Closed
    ) -> None:
        """Open the modal asking ``title`` with ``message``, offering ``choices``, the first
        picked on escape, calling ``closed`` with the index picked once the user closes it."""
        ...

    def close_modal(self, result: int | None) -> None:
        """Close the modal, if it's open, with ``result``, calling back nothing."""
        ...


class LinkMonitor:
    """Follows the link to the control plane, showing it in ``view``: while it's down, the
    status line and, after two minutes, a modal asking whether to keep trying, as one does at
    once when the control plane stops; a link that won't be retried is shown with only the
    choice to quit. ``quit`` ends the app, and ``after`` calls back after a delay."""

    def __init__(self, view: LinkView, quit: Callable[[], None], after: After) -> None:
        self._view = view
        self._quit = quit
        self._after = after
        self._timer: Timer | None = None  # Until the user is asked whether to keep trying.

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
        self._view.show_status(None)
        self._stop_timer()
        self._view.close_modal(0)

    def _reconnecting(self, url: str, delay: float) -> None:
        """Show that the link to ``url`` is being retried, asking whether to keep trying in
        ``delay`` seconds unless that's already due."""
        self._view.show_status(reconnecting_status(url))
        if self._timer is None:
            ask = lambda: self._ask(lost_title(url), lost_message(url))  # noqa: E731
            self._timer = self._after(max(delay, _SOONEST), ask)

    def _final(self, title: str, message: str) -> None:
        """Show ``title`` with ``message``, saying why the link won't be retried, quitting once
        it's closed, in place of any modal open."""
        self._view.show_status(None)
        self._stop_timer()
        self._view.close_modal(None)
        self._view.open_modal(title, message, (QUIT,), self._quit_now)

    def _ask(self, title: str, message: str) -> None:
        """Ask ``title`` with ``message`` whether to keep trying, unless that's being asked."""
        if self._view.modal_open:
            return
        self._view.open_modal(title, message, CHOICES, self._answered)

    async def _answered(self, index: int | None) -> None:
        """Quit if the choice at ``index`` is to."""
        if index is not None and CHOICES[index] == QUIT:
            self._quit()

    async def _quit_now(self, _: int | None) -> None:
        """Quit, whatever was picked."""
        self._quit()

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
