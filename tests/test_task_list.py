from textual.pilot import Pilot

from orbit.protocol import (
    CancelTurn,
    Event,
    RunTasks,
    Settings,
    TaskFinished,
    TaskSpec,
    TaskStarted,
    TurnFinished,
    TurnStarted,
)
from orbit.tui import OrbitApp
from orbit.tui.tasks import TaskEditorScreen, TaskList
from orbit.tui.widgets import Entry, PickerScreen, PromptInput
from tests.test_tui import FakeSession, picker, selected, show_settings


def rows(app: OrbitApp) -> list[str]:
    options = app.query_one(TaskList)
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


def editor(app: OrbitApp) -> TaskEditorScreen:
    screen = app.screen
    assert isinstance(screen, TaskEditorScreen)
    return screen


def editor_fields(app: OrbitApp) -> tuple[str, str, str]:
    """The open editor's prompt text, model and tools values."""
    screen = editor(app)
    return (
        screen.query_one(PromptInput).value,
        str(screen.query_one("#task-model").render()),
        str(screen.query_one("#task-tools").render()),
    )


async def feed(session: FakeSession, pilot: Pilot[None], *events: Event) -> None:
    for event in events:
        await session.queue.put(event)
    await pilot.pause()


async def open_task_list(pilot: Pilot[None]) -> None:
    """From the default selection (prompt), go to the task list and activate it."""
    await pilot.press("l", "k", "enter")


async def add_task(pilot: Pilot[None], text: str, *keys: str) -> None:
    """With the task list active on the new-task row: create a task typing ``text``."""
    await pilot.press("enter", "i", *text, "escape", *keys, "ctrl+s")


async def test_task_list_starts_with_only_the_new_task_row() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test():
        assert rows(app) == ["+ new task"]


async def test_enter_activates_the_task_list() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        tasks = app.query_one(TaskList)
        await pilot.press("l", "k")
        selected_border = tasks.styles.border_top
        await pilot.press("enter")
        assert app.focused is tasks
        assert tasks.highlighted == 0
        assert tasks.styles.border_top != selected_border  # active, not just selected

        # h/j/k/l don't leave the list while it's active; esc does.
        await pilot.press("h", "l", "i")
        assert selected(app) == ["task-list"]
        assert app.focused is tasks
        await pilot.press("escape")
        assert app.focused is None
        await pilot.press("h")
        assert selected(app) == ["transcript"]


async def test_new_task_editor_defaults_to_session_settings() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings(model="opus", tool_access="off"))
        await open_task_list(pilot)
        await pilot.press("enter")
        assert editor(app).title_text == "New task"
        assert editor_fields(app) == ("", "opus", "off")
        assert app.focused is None


async def test_editor_builds_a_task_with_vim_keys() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings())
        await open_task_list(pilot)
        await pilot.press("enter")
        screen = editor(app)

        # The prompt field needs `i` before typing; letters do nothing otherwise.
        await pilot.press("x")
        assert editor_fields(app)[0] == ""
        await pilot.press("i", *"fix it", "escape")
        assert editor_fields(app)[0] == "fix it"
        assert app.focused is None

        # j moves to the model field; enter opens a picker that j/k drive.
        await pilot.press("j")
        assert [w.id for w in screen.query(".selected")] == ["task-model"]
        await pilot.press("enter")
        assert picker(app)[1] == ["default", "opus", "sonnet"]
        await pilot.press("j", "enter")
        assert editor(app) is screen

        await pilot.press("j", "enter")
        assert picker(app)[0] == "Tool access"
        assert picker(app)[2] == 1  # restricted
        await pilot.press("j", "enter")
        assert editor_fields(app) == ("fix it", "opus", "full")

        # i jumps back to the prompt field from anywhere.
        await pilot.press("i", "!", "enter")
        assert editor_fields(app)[0] == "fix it!"
        assert [w.id for w in screen.query(".selected")] == ["task-prompt"]
        assert app.focused is None  # enter stops typing rather than submitting
        assert session.commands == []

        await pilot.press("k", "k", "j", "j", "ctrl+s")
        assert not isinstance(app.screen, TaskEditorScreen)
        assert rows(app) == ["○ fix it!\n  opus · full", "+ new task"]
        # Ready to add the next one.
        assert app.query_one(TaskList).highlighted == 1
        assert app.focused is app.query_one(TaskList)


async def test_editor_escape_discards_and_empty_prompt_is_not_saved() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        await open_task_list(pilot)
        await pilot.press("enter", "i", *"draft", "escape", "escape")
        assert not isinstance(app.screen, TaskEditorScreen)
        assert rows(app) == ["+ new task"]

        await pilot.press("enter", "ctrl+s")
        assert isinstance(app.screen, TaskEditorScreen)
        await pilot.press("i", " ", "escape", "ctrl+s")
        assert isinstance(app.screen, TaskEditorScreen)


async def test_editor_picker_escape_keeps_value() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings(model="sonnet"))
        await open_task_list(pilot)
        await pilot.press("enter", "j", "enter", "k", "escape")
        assert editor_fields(app)[1] == "sonnet"
        await pilot.press("k", "enter")
        assert not isinstance(app.screen, PickerScreen)  # enter on the prompt does nothing


async def test_editor_model_picker_highlights_nothing_for_custom_model() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings(model="claude-custom-1"))
        await open_task_list(pilot)
        await pilot.press("enter", "j", "enter")
        assert picker(app)[2] is None
        await pilot.press("escape")
        assert editor_fields(app)[1] == "claude-custom-1"


async def test_enter_on_existing_task_edits_it() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings())
        await open_task_list(pilot)
        await add_task(pilot, "one")
        await add_task(pilot, "two")
        assert rows(app)[:2] == ["○ one\n  default · restricted", "○ two\n  default · restricted"]

        await pilot.press("k", "k", "enter")
        assert editor(app).title_text == "Edit task"
        assert editor_fields(app)[0] == "one"
        await pilot.press("i", "!", "escape", "j", "j", "enter", "k", "enter", "ctrl+s")
        assert rows(app)[0] == "○ one!\n  default · off"
        assert app.query_one(TaskList).highlighted == 0


async def run_two_tasks(session: FakeSession, pilot: Pilot[None]) -> None:
    await show_settings(session, pilot, Settings())
    await open_task_list(pilot)
    await add_task(pilot, "one", "j", "enter", "j", "enter")
    await add_task(pilot, "two")
    await pilot.press("escape", "ctrl+r")


async def test_ctrl_r_runs_tasks_and_shows_progress() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await run_two_tasks(session, pilot)
        assert session.commands == [
            RunTasks(
                tasks=(
                    TaskSpec(prompt="one", settings=Settings(model="opus")),
                    TaskSpec(prompt="two"),
                )
            )
        ]
        prompt = app.query_one(PromptInput)
        assert prompt.disabled

        await feed(session, pilot, TaskStarted(index=0), TurnStarted(turn_id="a"))
        assert rows(app)[0].startswith("▶ one")
        assert [str(e.render()) for e in app.query(Entry)] == ["Task 1: one"]

        await feed(session, pilot, TurnFinished(turn_id="a", reason="completed"))
        assert prompt.disabled  # still running the batch

        await feed(
            session,
            pilot,
            TaskFinished(index=0, reason="completed"),
            TaskStarted(index=1),
            TurnStarted(turn_id="b"),
            TurnFinished(turn_id="b", reason="error"),
            TaskFinished(index=1, reason="error"),
        )
        assert [r.split("\n")[0] for r in rows(app)] == ["✓ one", "✗ two", "+ new task"]
        assert not prompt.disabled

        # Running again skips the completed task.
        await pilot.press("ctrl+r")
        assert session.commands[-1] == RunTasks(tasks=(TaskSpec(prompt="two"),))
        await feed(session, pilot, TaskStarted(index=0))
        assert rows(app)[1].startswith("▶ two")


async def test_cancelled_batch_ends_and_leaves_rest_pending() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await run_two_tasks(session, pilot)
        await pilot.press("escape")
        assert session.commands[-1] == CancelTurn()
        await feed(
            session,
            pilot,
            TaskStarted(index=0),
            TurnStarted(turn_id="a"),
            TurnFinished(turn_id="a", reason="cancelled"),
            TaskFinished(index=0, reason="cancelled"),
        )
        assert [r.split("\n")[0] for r in rows(app)] == ["■ one", "○ two", "+ new task"]
        assert not app.query_one(PromptInput).disabled


async def test_ctrl_r_does_nothing_without_tasks_or_while_busy() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings())
        await pilot.press("ctrl+r")
        assert session.commands == []

        await open_task_list(pilot)
        await add_task(pilot, "one")
        await pilot.press("escape", "i", *"hi", "enter", "ctrl+r")
        assert len(session.commands) == 1  # just the prompt; a turn is running

        await feed(
            session, pilot, TurnStarted(turn_id="t"), TurnFinished(turn_id="t", reason="completed")
        )
        await pilot.press("ctrl+r", "ctrl+r")
        assert [type(c) for c in session.commands[1:]] == [RunTasks]


async def test_editing_a_task_resets_it_to_pending() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings())
        await open_task_list(pilot)
        await add_task(pilot, "one")
        await pilot.press("escape", "ctrl+r")
        await feed(session, pilot, TaskStarted(index=0), TaskFinished(index=0, reason="completed"))
        assert rows(app)[0].startswith("✓")
        await pilot.press("enter", "k", "enter", "ctrl+s")
        assert rows(app)[0].startswith("○")


async def test_side_windows_do_not_take_focus_on_click() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        for window in ("#transcript", "#task-list", "#verification-steps"):
            await pilot.click(window)
            assert app.focused is None
        await pilot.press("k")
        assert selected(app) == ["transcript"]


async def test_clicking_a_row_of_the_active_list_opens_it() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        await open_task_list(pilot)
        await pilot.click("#task-list", offset=(3, 1))
        assert editor(app).title_text == "New task"
