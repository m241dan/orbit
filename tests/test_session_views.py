"""Read-only views of sessions: the mark by the prompt, the prompt to make the session viewed
the active one, and the refusals naming which read-only applies."""

from textual.widgets import OptionList, Static

from orbit.protocol import (
    BUSY,
    NOT_ACTIVE,
    ActivateSession,
    Activity,
    Attach,
    CommandRefused,
    RunTasks,
    SessionRef,
    SessionStateChanged,
    SessionSummary,
    Snapshot,
    TasksChanged,
    TaskSpec,
    TaskState,
)
from orbit.tui import OrbitApp
from orbit.tui.sessions import ActivationScreen
from orbit.tui.widgets import ChatPrompt
from tests.session_apps import CATALOG, CURRENT, OLD, RUNNING, catalog, joined, started
from tests.tui_fakes import entries, feed_events, opened

VIEWING_OLD = "viewing auth rework; today is idle (enter to switch)"
TODAY = SessionRef(id="now", name="today")
THIRD = SessionSummary(id="third", name="flags")
LEFT = SessionStateChanged(session="now", active=False, state="idle", viewers=0)
VIEWED = RUNNING.model_copy(update={"state": "idle", "viewers": 2})
"""The active session idle, but viewed by another frontend."""
RUNNING_T3 = RUNNING.model_copy(update={"activity": (Activity(kind="task_run", task_id="t3"),)})
"""The active session running its task ``t3``, an id the session viewed also numbers 3."""
REVIEWING_T3 = RUNNING.model_copy(update={"activity": (Activity(kind="review", task_id="t3"),)})
"""The active session reviewing its task ``t3``."""
IDLE = RUNNING.model_copy(update={"state": "idle"})
THREE_TASKS = TasksChanged(
    tasks=tuple(TaskState(id=f"t{n}", task=TaskSpec(prompt=f"task {n}")) for n in (1, 2, 3))
)


def view(summary: SessionSummary) -> Snapshot:
    """A read-only view of ``summary``'s session, while ``today`` is the active one."""
    return opened(summary).model_copy(update={"active": False, "open_session": TODAY})


def mark(app: OrbitApp) -> str | None:
    """The viewing mark by the prompt; none while it's hidden."""
    line = app.query_one("#viewing", Static)
    return str(line.render()) if line.display else None


def prompt(app: OrbitApp) -> ActivationScreen:
    screen = app.screen
    assert isinstance(screen, ActivationScreen)
    return screen


def choices(screen: ActivationScreen) -> list[str]:
    options = screen.query_one(OptionList)
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


def message(screen: ActivationScreen) -> str:
    return str(screen.query_one("#activation-message", Static).render())


async def test_a_view_shows_the_viewing_mark_naming_the_session() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, view(OLD))
        assert mark(app) == VIEWING_OLD


async def test_a_live_snapshot_removes_the_viewing_mark() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, view(OLD), opened(OLD))
        assert mark(app) is None


async def test_the_viewing_mark_follows_a_rename_in_the_catalog() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        renamed = OLD.model_copy(update={"name": "flags"})
        await feed_events(session, pilot, view(OLD), catalog(CURRENT, renamed))
        assert mark(app) == "viewing flags; today is idle (enter to switch)"


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
        assert session.commands == [
            Attach(workspace="w", session="old"),
            ActivateSession(session="old"),
        ]
        assert not isinstance(app.screen, ActivationScreen)


async def test_just_look_closes_the_prompt_and_keeps_the_view() -> None:
    app, session = joined()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG, VIEWED)
        await pilot.press("enter", "j", "enter")
        await feed_events(session, pilot, view(OLD))
        await pilot.press("j", "enter")

        assert session.commands == [Attach(workspace="w", session="old")]
        assert not isinstance(app.screen, ActivationScreen)
        assert mark(app) == VIEWING_OLD


async def test_escape_just_looks() -> None:
    app, session = joined()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG, VIEWED)
        await pilot.press("enter", "j", "enter")
        await feed_events(session, pilot, view(OLD))
        await pilot.press("escape")

        assert session.commands == [Attach(workspace="w", session="old")]
        assert not isinstance(app.screen, ActivationScreen)


async def test_with_the_active_session_running_the_prompt_only_offers_to_just_look() -> None:
    app, session = joined()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG, RUNNING_T3)
        await pilot.press("enter", "j", "enter")
        await feed_events(session, pilot, view(OLD))

        screen = prompt(app)
        assert choices(screen) == ["Just look"]
        assert message(screen) == "today is running a task; you can switch once it's idle."
        await pilot.press("enter")
        assert session.commands == [Attach(workspace="w", session="old")]
        assert not isinstance(app.screen, ActivationScreen)


async def test_a_session_activated_directly_offers_nothing() -> None:
    app, session = joined()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG)
        await pilot.press("enter", "j", "enter")
        await feed_events(session, pilot, opened(OLD))
        assert not isinstance(app.screen, ActivationScreen)


async def test_a_view_not_picked_here_offers_nothing_until_enter_on_its_mark() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG, view(OLD))
        assert not isinstance(app.screen, ActivationScreen)

        await pilot.press("enter")
        assert choices(prompt(app)) == ["Make this the active session", "Just look"]
        await pilot.press("enter")
        assert session.commands == [ActivateSession(session="old")]


async def test_enter_on_the_prompt_of_a_live_session_offers_nothing() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG, opened(CURRENT))
        await pilot.press("enter")
        assert not isinstance(app.screen, ActivationScreen)
        assert session.commands == []


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
        assert session.commands == [RunTasks()]
        text = (
            "Session 'auth rework' isn't the active session; 'today' is. Activate it to work in it."
        )
        refused = CommandRefused(
            command_id=session.envelopes[-1].command_id, code=NOT_ACTIVE, message=text
        )
        await feed_events(session, pilot, refused)
        assert entries(app)[-1] == (text, "error")


async def test_j_and_k_move_between_the_choices() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG, view(OLD))
        await pilot.press("enter", "j", "k", "enter")
        assert session.commands == [ActivateSession(session="old")]


async def test_a_state_change_activating_a_third_session_names_it_in_the_prompt() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        third = SessionStateChanged(session="third", active=True, state="idle", viewers=1)
        await feed_events(session, pilot, catalog(CURRENT, OLD, THIRD), view(OLD), LEFT, third)
        await pilot.press("enter")
        assert message(prompt(app)) == (
            "auth rework is a read-only view; flags is the active session, which runs work."
        )


async def test_a_state_change_running_a_third_session_names_it_in_the_prompt() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        third = SessionStateChanged(session="third", active=True, state="running", viewers=1)
        await feed_events(session, pilot, catalog(CURRENT, OLD, THIRD), view(OLD), LEFT, third)
        await pilot.press("enter")
        assert message(prompt(app)) == "flags is running work; you can switch once it's idle."
        assert choices(prompt(app)) == ["Just look"]


async def test_a_catalog_activating_a_third_session_names_it_in_the_prompt() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        left = CURRENT.model_copy(update={"active": False})
        third = THIRD.model_copy(update={"active": True})
        await feed_events(session, pilot, CATALOG, view(OLD), catalog(left, OLD, third))
        await pilot.press("enter")
        assert message(prompt(app)) == (
            "auth rework is a read-only view; flags is the active session, which runs work."
        )


async def test_the_prompt_names_the_session_viewed_as_renamed_in_the_catalog() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        renamed = OLD.model_copy(update={"name": "auth v2"})
        await feed_events(session, pilot, CATALOG, view(OLD), catalog(CURRENT, renamed))
        await pilot.press("enter")
        assert message(prompt(app)) == (
            "auth v2 is a read-only view; today is the active session, which runs work."
        )


async def test_a_live_snapshot_closes_the_prompt_shown_and_sends_nothing() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG, view(OLD))
        await pilot.press("enter")
        prompt(app)
        await feed_events(session, pilot, opened(OLD))
        assert not isinstance(app.screen, ActivationScreen)

        await pilot.press("enter")
        assert not isinstance(app.screen, ActivationScreen)
        assert session.commands == []


async def test_the_viewing_mark_names_what_the_active_session_runs_until_it_is_idle() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG, view(OLD), THREE_TASKS, RUNNING_T3)
        assert mark(app) == (
            "viewing auth rework; today is running a task (enter to switch when it's idle)"
        )
        await feed_events(session, pilot, IDLE)
        assert mark(app) == VIEWING_OLD


async def test_the_viewing_mark_names_a_review_without_the_viewed_sessions_numbers() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG, view(OLD), THREE_TASKS, REVIEWING_T3)
        assert mark(app) == (
            "viewing auth rework; today is running the review of a task"
            " (enter to switch when it's idle)"
        )


async def test_the_prompt_names_a_task_without_the_viewed_sessions_numbers() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG, view(OLD), THREE_TASKS, RUNNING_T3)
        await pilot.press("enter")
        assert message(prompt(app)) == "today is running a task; you can switch once it's idle."


async def test_a_pick_held_back_by_a_running_session_is_offered_again_once_it_is_idle() -> None:
    app, session = joined()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG, RUNNING_T3)
        await pilot.press("enter", "j", "enter")
        await feed_events(session, pilot, view(OLD))
        await pilot.press("enter")
        await feed_events(session, pilot, IDLE)

        screen = prompt(app)
        assert message(screen) == "today is idle now."
        assert choices(screen) == ["Make this the active session", "Just look"]
        await pilot.press("enter")
        assert session.commands == [
            Attach(workspace="w", session="old"),
            ActivateSession(session="old"),
        ]


async def test_the_offer_again_replaces_the_one_still_shown_and_comes_once() -> None:
    app, session = joined()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG, RUNNING_T3)
        await pilot.press("enter", "j", "enter")
        await feed_events(session, pilot, view(OLD), IDLE)

        [screen] = [s for s in app.screen_stack if isinstance(s, ActivationScreen)]
        assert message(screen) == "today is idle now."
        await pilot.press("escape")
        await feed_events(session, pilot, RUNNING_T3, IDLE)
        assert not isinstance(app.screen, ActivationScreen)
        assert session.commands == [Attach(workspace="w", session="old")]


async def test_a_view_not_picked_here_is_not_offered_again_when_the_active_one_is_idle() -> None:
    app, session = started()
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG, RUNNING_T3, view(OLD), IDLE)
        assert not isinstance(app.screen, ActivationScreen)
        assert mark(app) == VIEWING_OLD
