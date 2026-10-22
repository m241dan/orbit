"""A session halted because storage failed opens a modal and keeps a status line by the prompt."""

from textual.color import Color
from textual.containers import VerticalScroll
from textual.pilot import Pilot
from textual.widgets import Static

from orbit.protocol import (
    STORAGE_FAILED,
    BusyChanged,
    CommandAccepted,
    CommandRefused,
    ErrorOccurred,
    ResumeStorage,
    RunTasks,
    SessionSummary,
    Snapshot,
)
from orbit.tui import OrbitApp
from orbit.tui.storage_halt import StorageHaltScreen
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


def modal(app: OrbitApp) -> StorageHaltScreen:
    screen = app.screen
    assert isinstance(screen, StorageHaltScreen)
    return screen


def modal_text(app: OrbitApp) -> str:
    return str(modal(app).query_one("#text-body", Static).render())


def halt_screens(app: OrbitApp) -> list[StorageHaltScreen]:
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


async def test_j_k_and_the_arrows_scroll_a_long_explanation() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, ErrorOccurred(code=STORAGE_FAILED, message=LONG))
        box = modal(app).query_one(VerticalScroll)
        await pilot.press("j", "j", "down")
        assert box.scroll_y == 3
        await pilot.press("k", "up")
        assert box.scroll_y == 1


async def test_the_modal_is_bordered_as_an_error() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, HALT)
        _, colour = modal(app).query_one(VerticalScroll).styles.border_top
        assert colour == Color.parse(app.get_css_variables()["error"])


async def test_the_error_stays_in_the_transcript() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, HALT)
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


async def test_only_one_modal_opens_at_a_time() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, HALT, snapshot(STILL))
        assert len(halt_screens(app)) == 1
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


async def test_another_commands_acceptance_keeps_the_halt() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, HALT)
        await pilot.press("enter")
        await feed_events(session, pilot, CommandAccepted(command_id="other"))
        assert halt_screens(app) != [] and status(app) == EVENT_LOG_STATUS


async def test_an_earlier_try_agains_acceptance_keeps_the_halt() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, HALT)
        await pilot.press("enter")
        first = session.envelopes[-1].command_id
        await pilot.press("enter")
        assert session.commands == [ResumeStorage(), ResumeStorage()]
        await feed_events(session, pilot, CommandAccepted(command_id=first))
        assert halt_screens(app) != [] and status(app) == EVENT_LOG_STATUS


async def test_a_refused_try_again_shows_the_new_text() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, HALT)
        await pilot.press("enter")
        await refused(session, pilot, STILL)
        assert len(halt_screens(app)) == 1
        assert modal_text(app) == STILL
        assert status(app) == "Session stopped: session record failed; see the transcript"


async def test_a_refusal_for_another_reason_opens_nothing() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+r")
        command_id = session.envelopes[-1].command_id
        await feed_events(
            session, pilot, CommandRefused(command_id=command_id, code="busy", message="Busy")
        )
        assert halt_screens(app) == [] and status(app) is None


async def test_another_error_opens_nothing() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, ErrorOccurred(code="agent_failed", message="x"))
        assert halt_screens(app) == [] and status(app) is None


async def test_a_snapshot_with_a_halt_opens_the_modal() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, snapshot(EXPLANATION))
        assert modal_text(app) == EXPLANATION
        assert status(app) == EVENT_LOG_STATUS
        assert app.storage_halt == EXPLANATION


async def test_a_snapshot_without_a_halt_lifts_it() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, HALT)
        await pilot.press("escape")
        await feed_events(session, pilot, snapshot(None))
        assert status(app) is None and app.storage_halt is None


async def test_a_snapshot_without_a_halt_closes_the_modal() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, HALT, snapshot(None))
        assert halt_screens(app) == []


async def test_without_a_halt_nothing_opens_or_shows() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, snapshot(None), BusyChanged(busy=False))
        assert halt_screens(app) == [] and status(app) is None
        assert app.storage_halt is None


async def test_closing_the_modal_lets_other_screens_open() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, HALT)
        await pilot.press("escape", "ctrl+n")
        assert isinstance(app.screen, ConfirmScreen)


async def test_a_halt_after_the_try_agains_acceptance_reopens_the_modal() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, HALT)
        await pilot.press("enter")
        command_id = session.envelopes[-1].command_id
        again = ErrorOccurred(code=STORAGE_FAILED, message=STILL)
        await feed_events(session, pilot, CommandAccepted(command_id=command_id), again)
        assert len(halt_screens(app)) == 1
        assert modal_text(app) == STILL
        assert status(app) == "Session stopped: session record failed; see the transcript"


async def test_a_halt_after_a_snapshot_lifts_it_reopens_the_modal() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, HALT, snapshot(None), HALT)
        assert len(halt_screens(app)) == 1


async def test_a_closed_modal_leaves_the_one_opened_since_shown() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, HALT, snapshot(None), HALT)
        await feed_events(session, pilot, snapshot(STILL))
        assert len(halt_screens(app)) == 1
        assert modal_text(app) == STILL


async def test_a_halt_lifted_under_another_modal_closes_its_modal_and_leaves_the_other() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, HALT)
        other = ConfirmScreen("Other", "Another modal.", "yes", "no")
        app.push_screen(other)
        await feed_events(session, pilot, snapshot(None))
        assert halt_screens(app) == []
        assert app.screen is other
        assert app.storage_halt is None
