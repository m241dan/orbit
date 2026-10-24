"""Following the link to a control plane in another process, without the screens showing it:
the status line, when to ask whether to keep trying, and the notices that end the app."""

from collections.abc import Callable
from dataclasses import dataclass

import pytest

from orbit.protocol import (
    Connected,
    Ended,
    HandshakeFailed,
    LinkState,
    Reconnecting,
    Stopped,
    TokenRefused,
)
from orbit.tui.view.link import LOST_AFTER, Closed, LinkMonitor
from tests.server_fakes import URL

NOW = 1000.0
RECONNECTING = f"Reconnecting to {URL}…"
LOST = f"Lost the control plane at {URL}"
STOPPED = "The control plane stopped"


@dataclass
class Modal:
    """A modal the monitor opened: what it asks and offers, and what takes the choice the user
    closes it with."""

    title: str
    message: str
    choices: tuple[str, ...]
    closed: Closed


class View:
    """What the monitor shows, as it shows it: the status line and the modals opened, in turn;
    a test closes the one open as the user would, with :meth:`pick`. ``closes`` has the results
    the monitor closed modals with."""

    def __init__(self) -> None:
        self.status: str | None = None
        self.modals: list[Modal] = []
        self.modal_open = False
        self.closes: list[int | None] = []

    def show_status(self, status: str | None) -> None:
        self.status = status

    def open_modal(
        self, title: str, message: str, choices: tuple[str, ...], closed: Closed
    ) -> None:
        assert not self.modal_open
        self.modals.append(Modal(title, message, choices, closed))
        self.modal_open = True

    def close_modal(self, result: int | None) -> None:
        if self.modal_open:
            self.closes.append(result)
            self.modal_open = False

    async def pick(self, index: int | None) -> None:
        """Close the modal open, picking the choice at ``index``, or none."""
        assert self.modal_open
        self.modal_open = False
        await self.modals[-1].closed(index)


class Timer:
    """A call due after ``delay`` seconds, made with :meth:`fire`, unless stopped."""

    def __init__(self, delay: float, callback: Callable[[], None]) -> None:
        self.delay = delay
        self.callback = callback
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True

    def fire(self) -> None:
        assert not self.stopped
        self.callback()


class Clock:
    """The timers the monitor started, in turn."""

    def __init__(self) -> None:
        self.timers: list[Timer] = []

    def after(self, delay: float, callback: Callable[[], None]) -> Timer:
        timer = Timer(delay, callback)
        self.timers.append(timer)
        return timer


class Monitor:
    """A link monitor with its view and clock, counting how often it quit."""

    def __init__(self) -> None:
        self.view, self.clock = View(), Clock()
        self.quits = 0
        self.monitor = LinkMonitor(self.view, self._quit, self.clock.after)

    def _quit(self) -> None:
        self.quits += 1

    def follow(self, *states: LinkState) -> None:
        for state in states:
            self.monitor.follow(state, NOW)

    def lost(self) -> None:
        """Have the link lost two minutes ago, and the wait to ask run out."""
        self.follow(Reconnecting(URL, NOW - LOST_AFTER))
        self.clock.timers[-1].fire()


def test_the_status_line_shows_while_reconnecting() -> None:
    link = Monitor()
    link.follow(Connected(URL))
    assert link.view.status is None
    link.follow(Reconnecting(URL, NOW))
    assert link.view.status == RECONNECTING
    assert link.view.modals == []
    link.follow(Connected(URL))
    assert link.view.status is None


def test_reconnecting_asks_whether_to_keep_trying_once_two_minutes_have_gone_by() -> None:
    link = Monitor()
    link.follow(Reconnecting(URL, NOW - 20))
    [timer] = link.clock.timers
    assert timer.delay == LOST_AFTER - 20
    assert link.view.modals == []
    timer.fire()
    [modal] = link.view.modals
    assert (modal.title, modal.choices) == (LOST, ("Keep trying", "Quit"))
    assert modal.message == f"Orbit has been trying to reconnect to {URL} for two minutes."


def test_reconnecting_two_minutes_late_asks_as_soon_as_a_timer_can() -> None:
    link = Monitor()
    link.follow(Reconnecting(URL, NOW - LOST_AFTER - 500))
    assert [timer.delay for timer in link.clock.timers] == [0.01]


async def test_keeping_trying_closes_the_lost_modal_and_asks_once_an_outage() -> None:
    link = Monitor()
    link.lost()
    await link.view.pick(0)
    assert link.quits == 0
    link.follow(Reconnecting(URL, NOW - LOST_AFTER))  # Still the same outage: asked once.
    assert len(link.view.modals) == 1 and len(link.clock.timers) == 1
    assert link.view.status == RECONNECTING


async def test_quitting_from_the_lost_modal_quits() -> None:
    link = Monitor()
    link.lost()
    await link.view.pick(1)
    assert link.quits == 1


def test_reconnecting_in_time_closes_the_modal_and_stops_waiting() -> None:
    link = Monitor()
    link.lost()
    link.follow(Connected(URL))
    assert link.view.closes == [0]
    assert link.clock.timers[0].stopped
    link.follow(Reconnecting(URL, NOW))  # A new outage waits anew.
    assert link.clock.timers[1].delay == LOST_AFTER
    assert len(link.view.modals) == 1
    link.follow(Connected(URL))
    assert link.clock.timers[1].stopped


def test_connecting_with_no_outage_stops_and_closes_nothing() -> None:
    link = Monitor()
    link.follow(Connected(URL))
    assert link.view.closes == [] and link.clock.timers == []


def test_a_stopped_control_plane_asks_at_once_and_a_lost_link_behind_it_asks_nothing() -> None:
    link = Monitor()
    link.follow(Stopped(URL))
    [modal] = link.view.modals
    assert (modal.title, modal.choices) == (STOPPED, ("Keep trying", "Quit"))
    assert modal.message == (
        f"The control plane at {URL} is shutting down. Orbit keeps trying to reconnect."
    )
    link.lost()  # Behind it, not a second modal.
    assert len(link.view.modals) == 1 and link.view.status == RECONNECTING
    link.follow(Connected(URL))
    assert not link.view.modal_open and link.view.status is None


async def test_a_stop_right_after_reconnecting_asks_and_doesnt_quit() -> None:
    link = Monitor()
    link.lost()
    link.follow(Connected(URL), Stopped(URL))
    assert [modal.title for modal in link.view.modals] == [LOST, STOPPED]
    assert link.view.modal_open and link.quits == 0


async def test_a_stop_while_one_is_asked_about_asks_nothing_more() -> None:
    link = Monitor()
    link.follow(Stopped(URL), Stopped(URL))
    assert len(link.view.modals) == 1
    await link.view.pick(0)
    assert link.quits == 0


async def test_escape_keeps_trying_and_quit_quits_from_the_stopped_modal() -> None:
    link = Monitor()
    link.follow(Stopped(URL))
    await link.view.pick(0)  # The first choice, as escape picks.
    assert link.quits == 0 and not link.view.modal_open
    link.follow(Stopped(URL))
    await link.view.pick(1)
    assert link.quits == 1


async def test_a_modal_closed_with_no_choice_doesnt_quit() -> None:
    link = Monitor()
    link.follow(Stopped(URL))
    await link.view.pick(None)
    assert link.quits == 0


@pytest.mark.parametrize("picked", [0, None])
async def test_a_refused_token_shows_why_and_quits_once_closed(picked: int | None) -> None:
    link = Monitor()
    link.follow(Reconnecting(URL, NOW))
    link.follow(TokenRefused("The control plane refused the token from /t."))
    [modal] = link.view.modals
    assert modal.choices == ("Quit",)
    assert modal.title == "Can't connect"
    assert modal.message == "The control plane refused the token from /t."
    assert link.view.status is None
    assert link.clock.timers[0].stopped
    await link.view.pick(picked)
    assert link.quits == 1


async def test_a_failed_handshake_shows_why_and_quits_once_closed() -> None:
    link = Monitor()
    link.follow(HandshakeFailed(URL, "The server is speaking TLS. Use wss://."))
    [modal] = link.view.modals
    assert modal.choices == ("Quit",)
    assert modal.title == "Can't connect"
    assert modal.message == "The server is speaking TLS. Use wss://."
    await link.view.pick(0)
    assert link.quits == 1


@pytest.mark.parametrize(
    "reason",
    ["An event of 99 bytes is over the 9-byte limit", "An event can't be read: Not JSON"],
)
async def test_an_ended_link_shows_why_and_quits_once_closed(reason: str) -> None:
    link = Monitor()
    link.follow(Reconnecting(URL, NOW))
    link.follow(Ended(URL, reason))
    [modal] = link.view.modals
    assert modal.choices == ("Quit",)
    assert modal.title == "Disconnected"
    assert modal.message == (
        f"The connection to the control plane at {URL} ended: {reason}."
        " Reconnecting would end the same way."
    )
    assert link.view.status is None
    await link.view.pick(0)
    assert link.quits == 1


def test_a_final_notice_replaces_the_modal_asking() -> None:
    link = Monitor()
    link.follow(Stopped(URL), TokenRefused("Refused."))
    assert link.view.closes == [None]
    assert [modal.choices for modal in link.view.modals] == [("Keep trying", "Quit"), ("Quit",)]
    assert link.view.modal_open
