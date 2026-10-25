"""A paused run as the app shows it: the line by the prompt while the session viewed's run waits
to start again, and the same in that session's row of the picker; the wording is in
test_view_sessions."""

from datetime import datetime

from textual.widgets import OptionList, Static

from orbit.protocol import PAUSED_FOR_API_UNAVAILABLE
from orbit.tui import Opening, OrbitApp
from orbit.tui.sessions import SessionsScreen
from tests.navigation_fakes import fed, navigating
from tests.session_apps import CURRENT, OLD, RUNNING, catalog, started
from tests.tui_fakes import feed_events, opened

UNTIL = datetime(2026, 10, 23, 14, 20).astimezone()
"""When the paused run starts again: 14:20, local time."""

PAUSED_WORDS = "run paused: Claude's API isn't answering; trying again at 14:20"

PAUSE = {"paused_until": UNTIL, "paused_for": PAUSED_FOR_API_UNAVAILABLE}

PAUSED = RUNNING.model_copy(update={"state": "idle", **PAUSE})
"""The active session idle, its run paused as Claude's API isn't answering."""

PAUSED_CATALOG = catalog(CURRENT.model_copy(update=PAUSE), OLD)
"""The sessions, the active one's run paused."""


def paused_line(app: OrbitApp) -> str | None:
    """The line by the prompt saying the run is paused; none while it's hidden."""
    line = app.query_one("#paused", Static)
    return str(line.render()) if line.display else None


def picker_rows(app: OrbitApp) -> list[str]:
    screen = app.screen
    assert isinstance(screen, SessionsScreen)
    options = screen.query_one(OptionList)
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


async def test_the_session_viewed_says_its_run_is_paused() -> None:
    navigation, modals, backend = navigating()
    await fed(navigation, modals, backend, PAUSED_CATALOG)
    assert navigation.place.paused == PAUSED_WORDS


async def test_no_session_viewed_says_nothing_of_a_pause() -> None:
    navigation, modals, backend = navigating(Opening(pick=True), linked=True)
    await fed(navigation, modals, backend, PAUSED_CATALOG)
    assert navigation.place.paused is None


async def test_the_line_by_the_prompt_shows_the_pause_until_it_is_cleared() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, catalog(CURRENT, OLD), opened(CURRENT))
        before = paused_line(app)
        await feed_events(session, pilot, PAUSED)
        during = paused_line(app)
        await feed_events(session, pilot, RUNNING)
        assert (before, during, paused_line(app)) == (None, PAUSED_WORDS, None)


async def test_the_pickers_row_for_the_paused_session_shows_the_pause() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, catalog(CURRENT, OLD), opened(CURRENT), PAUSED)
        await pilot.press("ctrl+p")
        assert picker_rows(app)[:2] == [
            f"● today · Oct 16 18:02 · active · {PAUSED_WORDS}",
            "  auth rework · Oct 16 18:02",
        ]
