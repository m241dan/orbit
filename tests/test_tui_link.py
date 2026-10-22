"""How the TUI shows its link to a control plane in another process."""

import pytest
from textual.app import App
from textual.widgets import OptionList, Static

from orbit.protocol import (
    PLANNER,
    Attach,
    Catalog,
    Connected,
    Ended,
    HandshakeFailed,
    NewSession,
    NotConnected,
    Reconnecting,
    SendMessage,
    SessionSummary,
    Stopped,
    TokenRefused,
    Welcome,
    WorkspaceEntry,
)
from orbit.tui import Opening, OrbitApp
from orbit.tui.link import LinkHost
from orbit.tui.modal import Closed
from orbit.tui.view.link import LOST_AFTER, ended_message, lost_delay
from orbit.tui.widgets import ChatPrompt, ChoiceScreen
from tests.tui_fakes import FakeLink, FakeSession, entries, feed_events

URL = "ws://127.0.0.1:7433/"
NOW = 1000.0
WELCOME = Welcome(command_id="hello", server_version="1.0")
OLD = SessionSummary(id="old", name="earlier")


def catalog(*sessions: SessionSummary) -> Catalog:
    workspace = WorkspaceEntry(id="w", label="project", sessions=sessions)
    return Catalog(workspaces=(workspace,), agents=(), triggers=(), lanes=())


CATALOG = catalog(SessionSummary(id="now", name="today", active=True), OLD)


def linked() -> tuple[OrbitApp, FakeSession, FakeLink]:
    session, link = FakeSession(), FakeLink()
    app = OrbitApp(session, link)
    app.clock = lambda: NOW
    return app, session, link


def status(app: OrbitApp) -> str | None:
    """The link's status line, if it's shown."""
    line = app.query_one("#link-status", Static)
    return str(line.render()) if line.display else None


def modal(app: OrbitApp) -> ChoiceScreen | None:
    """The link's modal, if it's the screen on top."""
    return app.screen if isinstance(app.screen, ChoiceScreen) else None


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
        assert entries(app) == [("Not connected", "error")]
        prompt = app.query_one(ChatPrompt)
        assert prompt.text == "hi"
        assert prompt.border_title == "Prompt · planner"
        session.send_error = None
        await pilot.press("i", "enter")
        assert session.commands[-1] == SendMessage(agent=PLANNER, text="hi")


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


async def test_a_stop_right_after_reconnecting_shows_its_modal() -> None:
    app, _, link = linked()
    async with app.run_test() as pilot:
        await link.move(pilot, lost_long_ago())
        for changed in link.watchers:  # Before the lost modal's close callback runs.
            changed(Connected(URL))
            changed(Stopped(URL))
        await pilot.pause()
        shown = modal(app)
        assert shown is not None and shown.title_text == "The control plane stopped"
        assert app.return_code is None


async def test_the_old_modals_late_callback_leaves_the_new_one_open() -> None:
    app, _, link = linked()
    async with app.run_test() as pilot:
        await link.move(pilot, lost_long_ago())
        for changed in link.watchers:
            changed(Connected(URL))
            changed(Stopped(URL))
        await pilot.pause()
        stopped = modal(app)
        assert stopped is not None
        await link.move(pilot, Stopped(URL))  # Still asked by the open one.
        assert app.screen is stopped
        await pilot.press("escape")
        assert modal(app) is None


async def test_the_stopped_modal_is_bordered_as_an_error() -> None:
    app, _, link = linked()
    async with app.run_test() as pilot:
        await link.move(pilot, Stopped(URL))
        stopped = modal(app)
        assert stopped is not None and stopped.has_class("error")


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


async def test_a_failed_handshake_shows_why_and_quits_once_closed() -> None:
    app, _, link = linked()
    async with app.run_test() as pilot:
        await link.move(pilot, HandshakeFailed(URL, "The server is speaking TLS. Use wss://."))
        shown = modal(app)
        assert shown is not None and shown.choices == ("Quit",)
        assert shown.title_text == "Can't connect"
        assert shown.message == "The server is speaking TLS. Use wss://."
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_code == 0


@pytest.mark.parametrize(
    "reason",
    ["An event of 99 bytes is over the 9-byte limit", "An event can't be read: Not JSON"],
)
async def test_an_ended_link_shows_why_and_quits_once_closed(reason: str) -> None:
    app, _, link = linked()
    async with app.run_test() as pilot:
        await link.move(pilot, Reconnecting(URL, NOW))
        await link.move(pilot, Ended(URL, reason))
        shown = modal(app)
        assert shown is not None and shown.choices == ("Quit",)
        assert shown.title_text == "Disconnected"
        assert shown.message == (
            f"The connection to the control plane at {URL} ended: {reason}."
            " Reconnecting would end the same way."
        )
        assert status(app) is None
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_code == 0


def test_an_ended_link_without_a_reason_says_so_plainly() -> None:
    assert ended_message(URL, "") == (
        f"The connection to the control plane at {URL} ended. Reconnecting would end the same way."
    )


async def test_the_opening_command_is_sent_once_attached_and_only_once() -> None:
    session = FakeSession()
    app = OrbitApp(session, opening=Opening(NewSession(name="auth")))
    async with app.run_test() as pilot:
        await feed_events(session, pilot, WELCOME)
        assert session.commands == []
        await feed_events(session, pilot, CATALOG)
        await feed_events(session, pilot, WELCOME, CATALOG)
        assert session.commands == [NewSession(name="auth")]
        assert [e.payload for e in session.sent[1:]] == [
            Attach(workspace="w", session="now"),
            NewSession(name="auth"),
        ]


async def test_an_opening_attach_that_fails_to_send_sends_no_opening_command() -> None:
    session = FakeSession()
    session.send_error = RuntimeError("the backend is gone")
    app = OrbitApp(session, opening=Opening(NewSession(name="auth")))
    async with app.run_test() as pilot:
        session.attached = True  # So the Attach is sent as any command, and fails.
        await feed_events(session, pilot, CATALOG)
        assert session.commands == [Attach(workspace="w", session="now")]


async def test_a_catalog_with_no_active_session_attaches_to_nothing() -> None:
    session = FakeSession()
    app = OrbitApp(session, opening=Opening(NewSession(name="auth")))
    async with app.run_test() as pilot:
        await feed_events(session, pilot, catalog(OLD))
        assert [e.payload for e in session.sent[1:]] == []


def test_the_lost_modal_waits_out_what_is_left_of_two_minutes() -> None:
    assert lost_delay(NOW, NOW) == LOST_AFTER
    assert lost_delay(NOW - 20, NOW) == LOST_AFTER - 20
    assert lost_delay(NOW - 500, NOW) == 0


class LinkApp(App[None]):
    """An app holding a LinkHost, recording each modal's callback and each quit."""

    def __init__(self) -> None:
        super().__init__()
        self.callbacks: list[Closed[int]] = []
        self.quits = 0
        self.host = LinkHost(self._push, self._quit, lambda _: None, self.set_timer)

    def _push(self, screen: ChoiceScreen, closed: Closed[int]) -> None:
        self.callbacks.append(closed)
        self.push_screen(screen, closed)

    def _quit(self) -> None:
        self.quits += 1

    def link_screens(self) -> list[ChoiceScreen]:
        return [screen for screen in self.screen_stack if isinstance(screen, ChoiceScreen)]


async def test_a_superseded_modals_late_quit_does_not_quit() -> None:
    app = LinkApp()
    async with app.run_test() as pilot:
        app.host.follow(Stopped(URL), NOW)
        await pilot.pause()
        app.host.follow(Connected(URL), NOW)
        app.host.follow(Stopped(URL), NOW)
        await pilot.pause()
        newer = app.host.shown
        await app.callbacks[0](1)  # Quit, picked on the closed modal.
        assert app.quits == 0
        assert app.host.shown is newer
        assert app.link_screens() == [newer]


async def test_a_final_notice_replaces_the_modal_asking_and_quits_once_closed() -> None:
    app = LinkApp()
    async with app.run_test() as pilot:
        app.host.follow(Stopped(URL), NOW)
        await pilot.pause()
        app.host.follow(TokenRefused("Refused."), NOW)
        await pilot.pause()
        [final] = app.link_screens()
        assert final.choices == ("Quit",) and app.host.shown is final
        await app.callbacks[0](1)
        assert app.quits == 0
        await pilot.press("enter")
        await pilot.pause()
        assert app.quits == 1
        assert app.link_screens() == []
