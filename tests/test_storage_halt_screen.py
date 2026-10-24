"""A session halted because storage failed opens a modal and keeps a status line by the prompt:
the app taking halts to them, and the host's modal, one at a time, in a bare app."""

from textual.app import App
from textual.color import Color
from textual.containers import VerticalScroll
from textual.pilot import Pilot
from textual.widgets import Static

from orbit.protocol import (
    STORAGE_FAILED,
    Command,
    CommandAccepted,
    CommandRefused,
    ErrorOccurred,
    ResumeStorage,
    RunTasks,
    SessionSummary,
    Snapshot,
)
from orbit.tui import OrbitApp
from orbit.tui.storage_halt import StorageHaltHost, StorageHaltScreen
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.storage import HALT_HINT, HALT_TITLE
from orbit.tui.widgets import ConfirmScreen
from tests.tui_fakes import FakeSession, entries, feed_events, opened

EXPLANATION = (
    "Orbit stopped this session: saving the event log to /s/orbit.db failed.\n"
    "The error (disk full): database or disk is full"
)
STILL = "Orbit stopped this session: saving the session record to /s/orbit.db failed."
EVENT_LOG_STATUS = "Session stopped: event log failed; see the transcript"
HALT = ErrorOccurred(code=STORAGE_FAILED, message=EXPLANATION)
LONG = "\n".join(f"line {n}" for n in range(100))


def modal(app: App[None]) -> StorageHaltScreen:
    screen = app.screen
    assert isinstance(screen, StorageHaltScreen)
    return screen


def modal_text(app: App[None]) -> str:
    return str(modal(app).query_one("#text-body", Static).render())


def halt_screens(app: App[None]) -> list[StorageHaltScreen]:
    return [screen for screen in app.screen_stack if isinstance(screen, StorageHaltScreen)]


def status(app: OrbitApp) -> str | None:
    """The status line by the prompt, if it's shown."""
    line = app.query_one("#storage-halt", Static)
    return str(line.render()) if line.display else None


def snapshot(halt: str | None) -> Snapshot:
    return opened(SessionSummary(id="s", name="s")).model_copy(update={"storage_halt": halt})


async def refused(session: FakeSession, pilot: Pilot[None], message: str) -> None:
    """Refuse the last command sent with ``storage_failed`` and ``message``."""
    command_id = session.envelopes[-1].command_id
    await feed_events(
        session, pilot, CommandRefused(command_id=command_id, code=STORAGE_FAILED, message=message)
    )


async def test_a_storage_error_opens_the_modal_and_the_status_line() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, HALT)
        box = modal(app).query_one(VerticalScroll)
        assert (str(box.border_title), str(box.border_subtitle)) == (HALT_TITLE, HALT_HINT)
        assert modal_text(app) == EXPLANATION
        assert status(app) == EVENT_LOG_STATUS
        # The error stays in the transcript.
        assert any(EXPLANATION in text for text, _ in entries(app))


async def test_escape_closes_the_modal_and_the_status_line_stays() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, HALT)
        await pilot.press("escape")
        assert halt_screens(app) == []
        assert status(app) == EVENT_LOG_STATUS


async def test_a_command_refused_for_storage_reopens_the_modal_with_its_text() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, HALT)
        await pilot.press("escape", "ctrl+r")
        assert session.commands == [RunTasks()]
        await refused(session, pilot, STILL)
        assert modal_text(app) == STILL


async def test_enter_tries_again_and_its_acceptance_lifts_the_halt() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, HALT)
        await pilot.press("enter")
        assert session.commands == [ResumeStorage()]
        assert halt_screens(app) != []
        command_id = session.envelopes[-1].command_id
        await feed_events(session, pilot, CommandAccepted(command_id=command_id))
        assert halt_screens(app) == []
        assert status(app) is None
        assert app.storage_halt is None


async def test_a_snapshot_with_a_halt_opens_the_modal() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, snapshot(EXPLANATION))
        assert modal_text(app) == EXPLANATION
        assert status(app) == EVENT_LOG_STATUS
        assert app.storage_halt == EXPLANATION


async def test_a_snapshot_without_a_halt_closes_the_modal() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, HALT, snapshot(None))
        assert halt_screens(app) == []


async def test_closing_the_modal_lets_other_screens_open() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, HALT)
        await pilot.press("escape", "ctrl+n")
        assert isinstance(app.screen, ConfirmScreen)


class HaltApp(App[None]):
    """A bare app showing a StorageHaltHost's halt, recording the statuses it shows and the
    commands it sends."""

    def __init__(self) -> None:
        super().__init__()
        self.statuses: list[str | None] = []
        self.sent: list[Command] = []
        self.host = StorageHaltHost(self.push_screen, self._send, self.statuses.append)

    async def _send(self, command: Command, awaited: Awaited) -> None:
        self.sent.append(command)


async def test_j_k_and_the_arrows_scroll_a_long_explanation() -> None:
    app = HaltApp()
    async with app.run_test() as pilot:
        app.host.halt.halted(LONG)
        await pilot.pause()
        box = modal(app).query_one(VerticalScroll)
        await pilot.press("j", "j", "down")
        assert box.scroll_y == 3
        await pilot.press("k", "up")
        assert box.scroll_y == 1


async def test_the_modal_is_bordered_as_an_error() -> None:
    app = HaltApp()
    async with app.run_test() as pilot:
        app.host.halt.halted(EXPLANATION)
        await pilot.pause()
        _, colour = modal(app).query_one(VerticalScroll).styles.border_top
        assert colour == Color.parse(app.get_css_variables()["error"])


async def test_a_halt_while_the_modal_is_open_shows_in_it() -> None:
    app = HaltApp()
    async with app.run_test() as pilot:
        app.host.halt.halted(EXPLANATION)
        await pilot.pause()
        app.host.halt.halted(STILL)
        await pilot.pause()
        assert len(halt_screens(app)) == 1
        assert modal_text(app) == STILL
        assert app.host.modal_open


async def test_enter_in_the_modal_tries_again_and_leaves_it_open() -> None:
    app = HaltApp()
    async with app.run_test() as pilot:
        app.host.halt.halted(EXPLANATION)
        await pilot.pause()
        await pilot.press("enter")
        assert app.sent == [ResumeStorage()]
        assert len(halt_screens(app)) == 1
        assert app.statuses == [EVENT_LOG_STATUS]


async def test_a_closed_modal_leaves_the_one_opened_since_shown() -> None:
    app = HaltApp()
    async with app.run_test() as pilot:
        halt = app.host.halt
        halt.halted(EXPLANATION)
        halt.lifted()
        halt.halted(EXPLANATION)
        await pilot.pause()
        halt.halted(STILL)
        await pilot.pause()
        assert len(halt_screens(app)) == 1
        assert modal_text(app) == STILL


async def test_a_halt_lifted_under_another_modal_closes_its_modal_and_leaves_the_other() -> None:
    app = HaltApp()
    async with app.run_test() as pilot:
        app.host.halt.halted(EXPLANATION)
        await pilot.pause()
        other = ConfirmScreen("Other", "Another modal.", "yes", "no")
        app.push_screen(other)
        app.host.halt.lifted()
        await pilot.pause()
        assert halt_screens(app) == []
        assert app.screen is other
        assert not app.host.modal_open
        assert app.statuses == [EVENT_LOG_STATUS, None]


async def test_escape_closes_the_modal_so_the_next_halt_opens_another() -> None:
    app = HaltApp()
    async with app.run_test() as pilot:
        app.host.halt.halted(EXPLANATION)
        await pilot.pause()
        await pilot.press("escape")
        assert not app.host.modal_open
        assert halt_screens(app) == []
        app.host.halt.halted(STILL)
        await pilot.pause()
        assert len(halt_screens(app)) == 1
        assert modal_text(app) == STILL
