from textual.app import App
from textual.containers import VerticalScroll
from textual.message import Message
from textual.pilot import Pilot

from orbit.protocol import (
    AddTask,
    EditTask,
    Settings,
    SettingsChanged,
    TaskSpec,
)
from orbit.tui import OrbitApp
from orbit.tui.settings_pickers import SettingsPickers
from orbit.tui.task_editor import TaskEditorScreen
from orbit.tui.tasks import TaskList
from orbit.tui.widgets import PromptBox, PromptInput
from tests.tui_fakes import (
    FakeSession,
    board,
    feed_events,
    open_task_list,
    picker,
    rows,
    show_settings,
)


class EditorApp(App[None]):
    """An app opening just the task editor, on ``task`` or a new task, with the default
    settings, for what the editor does on its own; ``saved`` is what it closes with."""

    def __init__(self, task: TaskSpec | None) -> None:
        super().__init__()
        self.editing = task
        self.saved: list[TaskSpec | None] = []

    def on_mount(self) -> None:
        editor = TaskEditorScreen(self.editing, Settings(), SettingsPickers())
        self.push_screen(editor, self.saved.append)


def editor_screen(app: App[None]) -> TaskEditorScreen:
    """The open task editor."""
    screen = app.screen
    assert isinstance(screen, TaskEditorScreen)
    return screen


def editor_fields(app: App[None]) -> tuple[str, str, str, str]:
    """The open editor's title, prompt body, model and tools values."""
    screen = editor_screen(app)
    return (
        screen.query_one(PromptInput).value,
        screen.query_one(PromptBox).text,
        str(screen.query_one("#task-model").render()),
        str(screen.query_one("#task-tools").render()),
    )


async def test_new_task_editor_defaults_to_session_settings() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings(model="opus", tool_access="off"))
        await open_task_list(pilot)
        await pilot.press("enter")
        assert editor_screen(app).title_text == "New task"
        assert editor_fields(app) == ("", "", "opus", "off")
        assert app.focused is None
        assert editor_screen(app).query_one(PromptInput).border_title == "Title"
        assert editor_screen(app).query_one(PromptBox).border_title == "Prompt"
        assert editor_screen(app).query_one("#task-model").border_title == "Model"
        assert editor_screen(app).query_one("#task-tools").border_title == "Tool access"


async def test_editor_builds_a_task_with_vim_keys() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings())
        await open_task_list(pilot)
        await pilot.press("enter")
        screen = editor_screen(app)

        # The title field needs `i` before typing; letters do nothing otherwise.
        await pilot.press("x")
        assert editor_fields(app)[0] == ""
        await pilot.press("i", *"fix it", "escape")
        assert editor_fields(app)[0] == "fix it"
        assert app.focused is None

        # j moves past the prompt to the model field; enter opens a picker that j/k drive.
        await pilot.press("j", "j")
        assert [w.id for w in screen.query(".selected")] == ["task-model"]
        await pilot.press("enter")
        assert picker(app)[1] == ["default", "opus", "sonnet"]
        await pilot.press("j", "enter")
        assert editor_screen(app) is screen

        await pilot.press("j", "enter")
        assert picker(app)[0] == "Tool access"
        assert picker(app)[2] == 1  # restricted
        await pilot.press("j", "enter")
        assert editor_fields(app) == ("fix it", "", "opus", "full (auto-approved)")

        # i jumps back to the title field from the model or tools field.
        await pilot.press("i", "!", "enter")
        assert editor_fields(app)[0] == "fix it!"
        assert [w.id for w in screen.query(".selected")] == ["task-title"]
        assert app.focused is None  # enter stops typing rather than submitting
        assert session.commands == []
        await pilot.press("j", "j", "i")
        assert [w.id for w in screen.query(".selected")] == ["task-title"]
        assert app.focused is screen.query_one(PromptInput)
        await pilot.press("escape")

        # A title alone saves a single-line prompt, from whichever field is selected.
        await pilot.press("k", "k", "j", "j", "ctrl+s")
        assert not isinstance(app.screen, TaskEditorScreen)
        task = TaskSpec(prompt="fix it!", settings=Settings(model="opus", tool_access="full"))
        assert session.commands == [AddTask(task=task)]
        assert task.role == "coder"
        # The row appears once the backend reports it.
        assert rows(app) == ["+ new task"]
        assert app.focused is app.query_one(TaskList)


async def test_editor_escape_discards_and_blank_title_is_not_saved() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await open_task_list(pilot)
        await pilot.press("enter", "i", *"draft", "escape", "escape")
        assert not isinstance(app.screen, TaskEditorScreen)

        await pilot.press("enter", "ctrl+s")
        assert isinstance(app.screen, TaskEditorScreen)
        await pilot.press("i", " ", "escape", "ctrl+s")
        assert isinstance(app.screen, TaskEditorScreen)
        # A body is no title.
        await pilot.press("j", "i", *"body", "escape", "ctrl+s")
        assert isinstance(app.screen, TaskEditorScreen)
        assert editor_fields(app)[:2] == (" ", "body")
        assert session.commands == []


async def test_editor_shows_a_prompts_title_and_body_and_edits_the_body() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings())
        await feed_events(session, pilot, board("Add a widget\n\nPut it in widgets.py.\nTest it."))
        await open_task_list(pilot)
        await pilot.press("enter")
        screen = editor_screen(app)
        body = "Put it in widgets.py.\nTest it."
        assert editor_fields(app) == ("Add a widget", body, "default", "restricted")

        # j selects the prompt box; i types there, where enter adds a line.
        await pilot.press("j")
        assert [w.id for w in screen.query(".selected")] == ["task-prompt"]
        await pilot.press("enter")  # Nothing to choose on the prompt.
        assert editor_screen(app) is screen
        await pilot.press("i")
        assert app.focused is screen.query_one(PromptBox)
        await pilot.press("enter", *"more", "escape")
        assert app.focused is None
        assert editor_fields(app)[1] == f"{body}\nmore"
        await pilot.press("ctrl+s")
        edited = TaskSpec(prompt=f"Add a widget\n\n{body}\nmore")
        assert session.commands == [EditTask(task_id="1", task=edited)]


async def test_ctrl_s_saves_while_typing_in_the_prompt_box() -> None:
    app = EditorApp(None)
    async with app.run_test() as pilot:
        await pilot.press("i", *"Title", "escape", "j", "i", *"body", "ctrl+s")
        assert not isinstance(app.screen, TaskEditorScreen)
        assert app.saved == [TaskSpec(prompt="Title\n\nbody")]


async def test_ctrl_s_saves_while_typing_in_the_title() -> None:
    app = EditorApp(None)
    async with app.run_test() as pilot:
        await pilot.press("i", *"Title", "ctrl+s")
        assert not isinstance(app.screen, TaskEditorScreen)
        assert app.saved == [TaskSpec(prompt="Title")]


async def test_editor_model_picker_uses_choices_that_arrive_while_open() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await open_task_list(pilot)
        await pilot.press("enter", "i")
        await pilot.press(*"Add a widget", "escape")
        choices = ("opus", "sonnet", "haiku")
        await feed_events(
            session, pilot, SettingsChanged(settings=Settings(), model_choices=choices)
        )
        await pilot.press("j", "j", "enter", "j", "j", "j", "enter", "ctrl+s")
        task = TaskSpec(prompt="Add a widget", settings=Settings(model="haiku"))
        assert session.commands == [AddTask(task=task)]


async def test_editor_border_follows_the_selected_field() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await open_task_list(pilot)
        await pilot.press("enter")
        screen = editor_screen(app)
        title, model = screen.query_one("#task-title"), screen.query_one("#task-model")
        selected_border, unselected_border = title.styles.border_top, model.styles.border_top
        await pilot.press("j", "j")
        assert title.styles.border_top == unselected_border
        assert model.styles.border_top == selected_border != unselected_border


async def test_enter_edits_the_chosen_task_though_the_list_is_reordered_before_it_opens() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings())
        await feed_events(session, pilot, board("one", "two"))
        await open_task_list(pilot)
        tasks = app.query_one(TaskList)
        post = tasks.post_message
        reordered = tuple(reversed(tasks.tasks))

        def reorder_then_post(message: Message) -> bool:
            # The backend reorders the list after the row was picked, before the app opens it.
            if isinstance(message, TaskList.Edit):
                tasks.show(reordered)
            return post(message)

        tasks.post_message = reorder_then_post  # type: ignore[method-assign]
        await pilot.press("j", "enter")
        assert editor_fields(app)[0] == "two"
        await pilot.press("i", "!", "escape", "ctrl+s")
        assert session.commands == [EditTask(task_id="2", task=TaskSpec(prompt="two!"))]


async def test_enter_edits_the_chosen_task_though_the_list_is_reordered_right_after() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings())
        await feed_events(session, pilot, board("one", "two"))
        await open_task_list(pilot)
        tasks = app.query_one(TaskList)
        await pilot.press("j")
        # The backend reorders the list after enter, before any queued message is handled.
        tasks.action_select()
        tasks.show(tuple(reversed(tasks.tasks)))
        await pilot.pause()
        assert editor_fields(app)[0] == "two"
        await pilot.press("i", "!", "escape", "ctrl+s")
        assert session.commands == [EditTask(task_id="2", task=TaskSpec(prompt="two!"))]


async def test_clicking_a_task_row_edits_that_task() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings())
        await feed_events(session, pilot, board("one", "two"))
        await open_task_list(pilot)
        tasks = app.query_one(TaskList)
        top = tasks.row_lines()[1][0]
        await pilot.click(tasks, offset=(3, top + 1))
        assert editor_fields(app)[0] == "two"
        await pilot.press("i", "!", "escape", "ctrl+s")
        assert session.commands == [EditTask(task_id="2", task=TaskSpec(prompt="two!"))]


async def test_editor_scrolls_every_field_into_view_in_a_small_terminal() -> None:
    app = EditorApp(TaskSpec(prompt="Add a widget\n\n" + "\n".join(["line"] * 20)))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = editor_screen(app)
        box = screen.query_one(VerticalScroll)
        assert box.scroll_y == 0

        await pilot.press("j", "j", "j")
        assert box.scroll_y > 0
        assert box.content_region.contains_region(screen.query_one("#task-tools").region)

        await pilot.press("k", "k", "k")
        assert box.scroll_y == 0
        assert box.content_region.contains_region(screen.query_one("#task-title").region)


async def test_tab_does_not_focus_the_editor_scroll_container() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings())
        await feed_events(session, pilot, board("Add a widget"))
        await open_task_list(pilot)
        await pilot.press("enter")

        await pilot.press("tab")
        assert app.focused is None
        assert isinstance(app.screen, TaskEditorScreen)


LONG_PROMPT = "Add a widget\n\n" + "\n".join(f"line {n}" for n in range(40))


async def prompt_box(pilot: Pilot[None]) -> PromptBox:
    """The prompt box of the editor the app opens, once it's open."""
    await pilot.pause()
    return editor_screen(pilot.app).query_one(PromptBox)


def selected_fields(app: App[None]) -> list[str | None]:
    return [w.id for w in editor_screen(app).query(".selected")]


async def test_shift_j_and_k_scroll_the_prompt_box_one_line_at_a_time() -> None:
    app = EditorApp(TaskSpec(prompt=LONG_PROMPT))
    async with app.run_test() as pilot:
        box = await prompt_box(pilot)
        cursor = box.cursor_location

        await pilot.press("J", "J", "J")
        assert box.scroll_y == 3
        await pilot.press("K")
        assert box.scroll_y == 2
        assert box.cursor_location == cursor
        assert selected_fields(app) == ["task-title"]


async def test_shift_j_scrolls_the_prompt_box_from_every_field() -> None:
    app = EditorApp(TaskSpec(prompt=LONG_PROMPT))
    async with app.run_test() as pilot:
        box = await prompt_box(pilot)
        for field, expected in enumerate(["task-title", "task-prompt", "task-model", "task-tools"]):
            if field:
                await pilot.press("j")
            await pilot.press("J")
            assert box.scroll_y == field + 1
            assert selected_fields(app) == [expected]


async def test_shift_j_and_k_stop_at_the_ends_of_the_prompt_box() -> None:
    app = EditorApp(TaskSpec(prompt=LONG_PROMPT))
    async with app.run_test() as pilot:
        box = await prompt_box(pilot)
        await pilot.press("K")
        assert box.scroll_y == 0

        box.scroll_to(y=box.max_scroll_y, animate=False)
        await pilot.pause()
        assert box.scroll_y == box.max_scroll_y > 0
        await pilot.press("J")
        assert box.scroll_y == box.max_scroll_y


async def test_shift_j_and_k_do_nothing_when_the_prompt_fits() -> None:
    app = EditorApp(TaskSpec(prompt="Add a widget\n\nShort."))
    async with app.run_test() as pilot:
        box = await prompt_box(pilot)
        await pilot.press("J", "K", "J")
        assert box.scroll_y == 0


async def test_shift_j_and_k_type_in_the_prompt_while_inserting() -> None:
    app = EditorApp(TaskSpec(prompt=LONG_PROMPT))
    async with app.run_test() as pilot:
        box = await prompt_box(pilot)
        await pilot.press("j", "i")
        scroll_y = box.scroll_y
        await pilot.press("J", "K", "escape")
        assert box.text.endswith("line 39JK")
        assert box.scroll_y == scroll_y


async def test_shift_j_and_k_type_in_the_title_while_inserting() -> None:
    app = EditorApp(TaskSpec(prompt=LONG_PROMPT))
    async with app.run_test() as pilot:
        box = await prompt_box(pilot)
        await pilot.press("i", "J", "K", "escape")
        assert editor_fields(app)[0] == "Add a widgetJK"
        assert box.scroll_y == 0


async def test_editor_hint_mentions_shift_j_and_k_scrolling() -> None:
    app = EditorApp(TaskSpec(prompt=LONG_PROMPT))
    async with app.run_test() as pilot:
        await prompt_box(pilot)
        hint = editor_screen(app).query_one(VerticalScroll).border_subtitle
        assert hint == "j/k move · J/K scroll · i type · enter choose · ctrl+s save · esc cancel"
