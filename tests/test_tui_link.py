"""How the TUI shows its link to a control plane in another process."""

from collections.abc import Callable

from textual.pilot import Pilot
from textual.widgets import OptionList, Static

from orbit.protocol import (
    PLANNER,
    Connected,
    LinkState,
    NewSession,
    NotConnected,
    Reconnecting,
    SendMessage,
    Stopped,
    TokenRefused,
    Welcome,
)
from orbit.tui import OrbitApp
from orbit.tui.link import LinkScreen
from orbit.tui.view.link import LOST_AFTER, lost_delay
from tests.test_tui import FakeSession, entries, feed_events

URL = "ws://127.0.0.1:7433/"
NOW = 1000.0
WELCOME = Welcome(command_id="hello", server_version="1.0")


class FakeLink:
    """A link whose states a test moves it to."""

    def __init__(self) -> None:
        self.watchers: list[Callable[[LinkState], None]] = []

    def watch(self, changed: Callable[[LinkState], None]) -> None:
        self.watchers.append(changed)

    async def move(self, pilot: Pilot[None], state: LinkState) -> None:
        for changed in self.watchers:
            changed(state)
        await pilot.pause()


def linked() -> tuple[OrbitApp, FakeSession, FakeLink]:
    session, link = FakeSession(), FakeLink()
    app = OrbitApp(session, link)
    app.clock = lambda: NOW
    return app, session, link


def status(app: OrbitApp) -> str | None:
    """The link's status line, if it's shown."""
    line = app.query_one("#link-status", Static)
    return str(line.render()) if line.display else None


def modal(app: OrbitApp) -> LinkScreen | None:
    """The link's modal, if it's the screen on top."""
    return app.screen if isinstance(app.screen, LinkScreen) else None


def lost_long_ago() -> Reconnecting:
    return Reconnecting(URL, NOW - LOST_AFTER)


async def test_the_status_line_shows_while_reconnecting() -> None:
    app, _, link = linked()
    async with app.run_test() as pilot:
        await link.move(pilot, Connected(URL))
        assert status(app) is None
        await link.move(pilot, Reconnecting(URL, NOW))
        assert status(app) == f"Reconnecting to {URL}…"
        assert modal(app) is None
        await link.move(pilot, Connected(URL))
        assert status(app) is None


async def test_a_command_sent_while_disconnected_is_refused_as_not_connected() -> None:
    app, session, _ = linked()
    session.send_error = NotConnected()
    async with app.run_test() as pilot:
        await pilot.press("i", *"hi", "enter")
        await pilot.pause()
        assert entries(app)[-2:] == [("hi", "prompt"), ("Not connected", "error")]
        session.send_error = None
        await pilot.press("i", *"again", "enter")
        assert session.commands[-1] == SendMessage(agent=PLANNER, text="again")


async def test_after_two_minutes_of_reconnecting_a_modal_offers_to_keep_trying() -> None:
    app, _, link = linked()
    async with app.run_test() as pilot:
        await link.move(pilot, lost_long_ago())
        shown = modal(app)
        assert shown is not None and shown.title_text == f"Lost the control plane at {URL}"
        assert shown.choices == ("Keep trying", "Quit")
        assert shown.query_one(OptionList).highlighted == 0
        await pilot.press("enter")
        assert modal(app) is None and app.return_code is None
        await link.move(pilot, lost_long_ago())  # Still the same outage: asked once.
        assert modal(app) is None


async def test_quitting_from_the_lost_modal_exits() -> None:
    app, _, link = linked()
    async with app.run_test() as pilot:
        await link.move(pilot, lost_long_ago())
        await pilot.press("down", "enter")
        await pilot.pause()
    assert app.return_code == 0


async def test_reconnecting_in_time_closes_the_modal_and_stops_waiting() -> None:
    app, _, link = linked()
    async with app.run_test() as pilot:
        await link.move(pilot, lost_long_ago())
        await link.move(pilot, Connected(URL))
        assert modal(app) is None
        await link.move(pilot, Reconnecting(URL, NOW))  # A new outage waits anew.
        assert modal(app) is None
        await link.move(pilot, Connected(URL))


async def test_a_stopped_control_plane_shows_its_modal_at_once() -> None:
    app, _, link = linked()
    async with app.run_test() as pilot:
        await link.move(pilot, Stopped(URL))
        shown = modal(app)
        assert shown is not None and shown.title_text == "The control plane stopped"
        assert URL in shown.message
        await link.move(pilot, lost_long_ago())  # Behind it, not a second modal.
        assert app.screen is shown and status(app) == f"Reconnecting to {URL}…"
        await link.move(pilot, Connected(URL))
        assert modal(app) is None and status(app) is None


async def test_escape_keeps_trying_and_quit_exits_from_the_stopped_modal() -> None:
    app, _, link = linked()
    async with app.run_test() as pilot:
        await link.move(pilot, Stopped(URL))
        await pilot.press("escape")
        assert modal(app) is None
        await link.move(pilot, Stopped(URL))
        await pilot.press("down", "enter")
        await pilot.pause()
    assert app.return_code == 0


async def test_a_refused_token_shows_why_and_quits_once_closed() -> None:
    app, _, link = linked()
    async with app.run_test() as pilot:
        await link.move(pilot, Reconnecting(URL, NOW))
        await link.move(pilot, TokenRefused("The control plane refused the token from /t."))
        shown = modal(app)
        assert shown is not None and shown.choices == ("Quit",)
        assert shown.message == "The control plane refused the token from /t."
        assert status(app) is None
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_code == 0


async def test_the_opening_command_is_sent_once_welcomed_and_only_once() -> None:
    session = FakeSession()
    app = OrbitApp(session, opening=NewSession(name="auth"))
    async with app.run_test() as pilot:
        assert session.commands == []
        await feed_events(session, pilot, WELCOME)
        await feed_events(session, pilot, WELCOME)
        assert session.commands == [NewSession(name="auth")]


def test_the_lost_modal_waits_out_what_is_left_of_two_minutes() -> None:
    assert lost_delay(NOW, NOW) == LOST_AFTER
    assert lost_delay(NOW - 20, NOW) == LOST_AFTER - 20
    assert lost_delay(NOW - 500, NOW) == 0
