"""Read-only views of sessions: the mark by the prompt, the prompt to make the session viewed
the active one, and the refusals naming which read-only applies, as the app shows them; the
rules behind them are in test_viewing."""

from textual.widgets import OptionList, Static

from orbit.protocol import (
    BUSY,
    NOT_ACTIVE,
    ActivateSession,
    Attach,
    CommandRefused,
    RunTasks,
    SessionRef,
    SessionSummary,
    Snapshot,
)
from orbit.tui import OrbitApp
from orbit.tui.widgets import ChatPrompt, ChoiceScreen
from tests.session_apps import CATALOG, OLD, RUNNING, joined, started
from tests.tui_fakes import changes, entries, feed_events, opened

VIEWING_OLD = "viewing auth rework; today is idle (enter to switch)"
TODAY = SessionRef(id="now", name="today")
VIEWED = RUNNING.model_copy(update={"state": "idle", "viewers": 2})
"""The active session idle, but viewed by another frontend."""


def view(summary: SessionSummary) -> Snapshot:
    """A read-only view of ``summary``'s session, while ``today`` is the active one."""
    return opened(summary).model_copy(update={"active": False, "open_session": TODAY})


def mark(app: OrbitApp) -> str | None:
    """The viewing mark by the prompt; none while it's hidden."""
    line = app.query_one("#viewing", Static)
    return str(line.render()) if line.display else None


def prompt(app: OrbitApp) -> ChoiceScreen:
    screen = app.screen
    assert isinstance(screen, ChoiceScreen)
    return screen


def choices(screen: ChoiceScreen) -> list[str]:
    options = screen.query_one(OptionList)
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


def message(screen: ChoiceScreen) -> str:
    return str(screen.query_one("#choice-message", Static).render())


async def test_the_viewing_and_repository_marks_show_together() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, view(OLD).model_copy(update={"read_only": True}))
        assert mark(app) == VIEWING_OLD
        assert app.query_one(ChatPrompt).border_title == "Prompt · planner · read-only"


async def test_picking_a_session_viewed_read_only_offers_to_make_it_active() -> None:
    app, session = joined()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG, VIEWED)
        await pilot.press("enter", "j", "enter")
        await feed_events(session, pilot, view(OLD))

        screen = prompt(app)
        assert choices(screen) == ["Make this the active session", "Just look"]
        assert message(screen) == (
            "auth rework is a read-only view; today is the active session, which runs work."
        )
        await pilot.press("enter")
        assert changes(session) == [
            Attach(workspace="w", session="old"),
            ActivateSession(session="old"),
        ]
        assert not isinstance(app.screen, ChoiceScreen)


async def test_escape_just_looks() -> None:
    app, session = joined()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG, VIEWED)
        await pilot.press("enter", "j", "enter")
        await feed_events(session, pilot, view(OLD))
        await pilot.press("escape")

        assert changes(session) == [Attach(workspace="w", session="old")]
        assert not isinstance(app.screen, ChoiceScreen)


async def test_a_view_not_picked_here_offers_nothing_until_enter_on_its_mark() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG, view(OLD))
        assert not isinstance(app.screen, ChoiceScreen)

        await pilot.press("enter")
        assert choices(prompt(app)) == ["Make this the active session", "Just look"]
        await pilot.press("enter")
        assert changes(session) == [ActivateSession(session="old")]


async def test_a_busy_refusal_of_activating_shows_its_message() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG, view(OLD))
        await pilot.press("enter", "enter")
        refused = CommandRefused(
            command_id=session.envelopes[-1].command_id,
            code=BUSY,
            message="Can't switch sessions while tasks run.",
        )
        await feed_events(session, pilot, refused)
        assert entries(app)[-1] == ("Can't switch sessions while tasks run.", "error")


async def test_running_tasks_in_a_view_shows_the_refusal_naming_the_open_session() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG, view(OLD))
        await pilot.press("ctrl+r")
        assert changes(session) == [RunTasks()]
        text = (
            "Session 'auth rework' isn't the active session; 'today' is. Activate it to work in it."
        )
        refused = CommandRefused(
            command_id=session.envelopes[-1].command_id, code=NOT_ACTIVE, message=text
        )
        await feed_events(session, pilot, refused)
        assert entries(app)[-1] == (text, "error")


async def test_a_live_snapshot_closes_the_prompt_shown_and_sends_nothing() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG, view(OLD))
        await pilot.press("enter")
        prompt(app)
        await feed_events(session, pilot, opened(OLD))
        assert not isinstance(app.screen, ChoiceScreen)

        await pilot.press("enter")
        assert not isinstance(app.screen, ChoiceScreen)
        assert changes(session) == []
