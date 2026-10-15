from textual.pilot import Pilot

from orbit.protocol import (
    AcceptTask,
    AddTask,
    BusyChanged,
    EditTask,
    Event,
    PlanningChanged,
    RemoveTask,
    RequestSuperReview,
    RunTasks,
    SetPlanning,
    Settings,
    TasksChanged,
    TaskSpec,
    TaskStarted,
    TaskState,
    TaskStatus,
    TurnFinished,
    Usage,
)
from orbit.tui import OrbitApp
from orbit.tui.tasks import TaskEditorScreen, TaskList
from orbit.tui.view import task_label, task_numbers, usage_label
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


def board(*prompts: str, status: TaskStatus = "pending") -> TasksChanged:
    """A board whose tasks have ids "1", "2", … and the given prompts."""
    return TasksChanged(
        tasks=tuple(
            TaskState(id=str(i), task=TaskSpec(prompt=p), status=status)
            for i, p in enumerate(prompts, 1)
        )
    )


async def open_task_list(pilot: Pilot[None]) -> None:
    """From the default selection (prompt), go to the task list and activate it."""
    await pilot.press("l", "k", "enter")


def test_label_shows_status_title_and_settings() -> None:
    task = TaskSpec(prompt="Fix it\nlong details", settings=Settings(model="opus"))
    icons = {
        "pending": "⏳",
        "completed": "✅",
        "unverified": "❗",
        "cancelled": "🛑",
        "max_steps": "⌛",
        "error": "❌",
    }
    for status, icon in icons.items():
        state = TaskState(id="1", task=task, status=status)  # type: ignore[arg-type]
        # Long prompts (like fix tasks) are titled by their first line; settings line up
        # under the title.
        label = task_label(state, "1", "◠ ")
        assert str(label) == f"1. {icon} Fix it\n      opus · restricted"
    assert str(task_label(TaskState(id="1", task=TaskSpec(prompt="x")), "12", "◠ ")) == (
        "12. ⏳ x\n       default · restricted"
    )


def test_running_tasks_show_the_spinner_frame() -> None:
    running = TaskState(id="1", task=TaskSpec(prompt="go"), status="running")
    assert str(task_label(running, "1", "◠ ")) == "1. ◠  go\n      default · restricted"
    # Verifying shows the magnifying glass without a spinner; the verification panel spins.
    verifying = running.model_copy(update={"status": "verifying"})
    assert str(task_label(verifying, "1", "◠ ")) == "1. 🔍 go\n      default · restricted"


def test_usage_reads_as_tokens_and_cost() -> None:
    assert usage_label(Usage(input_tokens=512)) == "512 tok · $0.00"
    assert usage_label(Usage(input_tokens=12_000, output_tokens=345, cost_usd=0.1)) == (
        "12.3k tok · $0.10"
    )
    big = Usage(cache_read_input_tokens=1_200_000, output_tokens=34_567, cost_usd=0.836)
    assert usage_label(big) == "1.2M tok · $0.84"
    assert usage_label(Usage(input_tokens=999)) == "999 tok · $0.00"
    assert usage_label(Usage(input_tokens=1_000)) == "1.0k tok · $0.00"
    assert usage_label(Usage(input_tokens=999_949)) == "999.9k tok · $0.00"
    assert usage_label(Usage(input_tokens=999_950)) == "1.0M tok · $0.00"


def test_label_shows_what_a_task_spent_once_it_spent_anything() -> None:
    usage = Usage(cache_read_input_tokens=1_200_000, cost_usd=0.84)
    state = TaskState(id="1", task=TaskSpec(prompt="go"), status="completed", usage=usage)
    assert str(task_label(state, "1", "◠ ")) == (
        "1. ✅ go\n      default · restricted · 1.2M tok · $0.84"
    )


def test_fix_tasks_are_indented_under_their_task() -> None:
    fix = TaskState(id="2", task=TaskSpec(prompt="Fix it"), fixes="1")
    assert str(task_label(fix, "1.1", "◠ ")) == "   1.1 ⏳ Fix it\n          default · restricted"


def test_tasks_are_numbered_and_every_fix_in_a_chain_under_the_first_task() -> None:
    def state(task_id: str, fixes: str | None = None) -> TaskState:
        return TaskState(id=task_id, task=TaskSpec(prompt=task_id), fixes=fixes)

    tasks = [
        state("a"),
        state("b"),
        state("b-fix", fixes="b"),
        state("b-fix-fix", fixes="b-fix"),
        state("c"),
        state("c-fix", fixes="c"),
    ]
    assert task_numbers(tasks) == ["1", "2", "2.1", "2.2", "3", "3.1"]
    assert task_numbers([]) == []


async def test_task_list_starts_with_only_the_new_task_row() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test():
        assert rows(app) == ["+ new task"]


async def test_list_shows_the_board_the_backend_reports() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        one, two = board("one", "two", status="completed").tasks
        fix = TaskState(id="3", task=TaskSpec(prompt="fix one"), fixes="1")
        await feed(session, pilot, TasksChanged(tasks=(one, fix, two)))
        assert rows(app) == [
            "1. ✅ one\n      default · restricted",
            "   1.1 ⏳ fix one\n          default · restricted",
            "2. ✅ two\n      default · restricted",
            "+ new task",
        ]


async def test_running_tasks_spin_in_place() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        tasks = app.query_one(TaskList)
        now = [0.0]
        tasks.clock = lambda: now[0]
        one, two, three = board("one", "two", "three").tasks
        await feed(
            session,
            pilot,
            TasksChanged(
                tasks=(
                    one.model_copy(update={"status": "completed"}),
                    two.model_copy(update={"status": "running"}),
                    three.model_copy(update={"status": "verifying"}),
                )
            ),
        )
        await open_task_list(pilot)
        await pilot.press("j", "j")
        assert [r.split("\n")[0] for r in rows(app)[:3]] == [
            "1. ✅ one",
            "2. ◜  two",
            "3. 🔍 three",
        ]

        now[0] = 0.25  # two frames on, at 8 a second
        tasks.tick()
        assert rows(app)[1].startswith("2. ◝  two")
        assert rows(app)[2].startswith("3. 🔍 three")  # only running tasks spin
        assert tasks.highlighted == 2  # redrawing a row leaves the highlight alone

        # Nothing spins once nothing is in progress.
        await feed(session, pilot, board("one", "two", "three", status="completed"))
        before = rows(app)
        now[0] = 0.5
        tasks.tick()
        assert rows(app) == before


async def test_highlight_stays_on_its_task_as_the_board_changes() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed(session, pilot, board("one", "two"))
        await open_task_list(pilot)
        tasks = app.query_one(TaskList)
        await pilot.press("j")
        assert tasks.highlighted == 1  # on "two"

        # A fix task appears before it: the highlight follows "two" down.
        fix = TaskState(id="9", task=TaskSpec(prompt="fix"))
        one, two = board("one", "two").tasks
        await feed(session, pilot, TasksChanged(tasks=(one, fix, two)))
        assert tasks.highlighted == 2

        # On the new-task row, it stays there as tasks are added.
        await pilot.press("j")
        new = TaskState(id="10", task=TaskSpec(prompt="three"))
        await feed(session, pilot, TasksChanged(tasks=(one, fix, two, new)))
        assert tasks.highlighted == 4

        # Leaving and re-entering the list keeps the highlight where it was.
        await pilot.press("escape", "enter")
        assert tasks.highlighted == 4


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
        task = TaskSpec(prompt="fix it!", settings=Settings(model="opus", tool_access="full"))
        assert session.commands == [AddTask(task=task)]
        # The row appears once the backend reports it.
        assert rows(app) == ["+ new task"]
        assert app.focused is app.query_one(TaskList)


async def test_editor_escape_discards_and_empty_prompt_is_not_saved() -> None:
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
        assert session.commands == []


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
        await feed(session, pilot, board("one", "two"))
        await open_task_list(pilot)
        await pilot.press("j", "enter")
        assert editor(app).title_text == "Edit task"
        assert editor_fields(app)[0] == "two"
        await pilot.press("i", "!", "escape", "j", "j", "enter", "k", "enter", "ctrl+s")
        edited = TaskSpec(prompt="two!", settings=Settings(tool_access="off"))
        assert session.commands == [EditTask(task_id="2", task=edited)]


async def test_ctrl_r_asks_the_backend_to_run_unless_busy() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+r")
        assert session.commands == [RunTasks()]

        await feed(session, pilot, BusyChanged(busy=True))
        await pilot.press("ctrl+r")
        assert session.commands == [RunTasks()]

        await feed(session, pilot, BusyChanged(busy=False))
        await pilot.press("ctrl+r")
        assert session.commands == [RunTasks(), RunTasks()]


async def test_started_task_is_announced_in_the_transcript() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        one, two = board("one", "two").tasks
        fix = TaskState(id="3", task=TaskSpec(prompt="fix one"), fixes="1")
        await feed(
            session,
            pilot,
            TasksChanged(tasks=(one, fix, two)),
            TaskStarted(task_id="2"),
            TaskStarted(task_id="3"),
        )
        entries = list(app.query(Entry))
        # Announced by the number the list shows.
        assert [str(e.render()) for e in entries] == ["Task 2: two", "Task 1.1: fix one"]
        assert entries[0].has_class("user")


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


def test_tasks_being_fixed_show_which_fix_they_are_on() -> None:
    state = TaskState(
        id="1", task=TaskSpec(prompt="go"), status="running", attempt=2, max_attempts=5
    )
    assert str(task_label(state, "1", "◠ ")) == "1. ◠  go\n      default · restricted · fix 2/5"


async def test_ctrl_g_asks_for_a_super_review_even_while_busy() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed(session, pilot, BusyChanged(busy=True))
        await pilot.press("ctrl+g")
        assert session.commands == [RequestSuperReview()]


async def test_a_accepts_the_highlighted_draft_and_x_removes_a_task() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        draft = TaskState(id="1", task=TaskSpec(prompt="plan step"), status="draft")
        queued = TaskState(id="2", task=TaskSpec(prompt="queued"))
        await feed(session, pilot, TasksChanged(tasks=(draft, queued)))
        await open_task_list(pilot)
        await pilot.press("a")
        await pilot.press("j", "a")  # Not a draft: nothing to accept.
        await pilot.press("x")
        await pilot.press("j", "a", "x")  # The new-task row.
        assert session.commands == [AcceptTask(task_id="1"), RemoveTask(task_id="2")]


async def test_ctrl_t_toggles_planning_and_the_prompt_says_so() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+t")
        assert session.commands == [SetPlanning(on=True)]
        await feed(session, pilot, PlanningChanged(on=True))
        assert app.query_one(PromptInput).border_title == "Prompt · planner"
        await pilot.press("ctrl+t")
        assert session.commands[-1] == SetPlanning(on=False)
        await feed(session, pilot, PlanningChanged(on=False))
        assert app.query_one(PromptInput).border_title == "Prompt"


async def test_list_shows_each_tasks_usage_and_the_sessions_total() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        assert not app.query_one(TaskList).border_subtitle
        spent = TaskState(
            id="1", task=TaskSpec(prompt="one"), usage=Usage(input_tokens=1_500, cost_usd=0.2)
        )
        await feed(session, pilot, TasksChanged(tasks=(spent,)))
        assert rows(app)[0].endswith("default · restricted · 1.5k tok · $0.20")
        # Every metered turn counts towards the session, whether or not a task ran it.
        await feed(
            session,
            pilot,
            TurnFinished(turn_id="a", reason="completed", usage=Usage(input_tokens=1_000)),
            TurnFinished(turn_id="b", reason="completed"),
            TurnFinished(
                turn_id="c",
                reason="error",
                agent_id="agent-1",
                usage=Usage(output_tokens=500, cost_usd=0.3),
            ),
        )
        assert app.query_one(TaskList).border_subtitle == "session: 1.5k tok · $0.30"
