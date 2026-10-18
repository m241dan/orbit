import pytest
from rich.cells import cell_len
from textual import events
from textual.pilot import Pilot

from orbit.agent import AgentSession
from orbit.protocol import (
    NOT_CONFIGURED,
    SUPER_REVIEW,
    AcceptTask,
    BusyChanged,
    ErrorOccurred,
    Event,
    FireTrigger,
    OpenSession,
    RemoveTask,
    RunTasks,
    SessionsListed,
    SessionSummary,
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
from orbit.tui.sessions import SessionsScreen
from orbit.tui.task_editor import TaskEditorScreen
from orbit.tui.tasks import TaskList
from orbit.tui.transcript import Transcript
from orbit.tui.view.common import spinner_frame
from orbit.tui.view.tasks import task_label, task_numbers, usage_label
from orbit.tui.widgets import ChatPrompt
from tests.test_tui import FakeSession, opened, selected, show_settings


def rows(app: OrbitApp) -> list[str]:
    options = app.query_one(TaskList)
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


def editor(app: OrbitApp) -> TaskEditorScreen:
    screen = app.screen
    assert isinstance(screen, TaskEditorScreen)
    return screen


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
        "12k tok · $0.10"
    )
    big = Usage(cache_read_input_tokens=1_200_000, output_tokens=34_567, cost_usd=0.836)
    assert usage_label(big) == "1.2M tok · $0.84"
    assert usage_label(Usage(input_tokens=999)) == "999 tok · $0.00"
    assert usage_label(Usage(input_tokens=1_000)) == "1k tok · $0.00"
    assert usage_label(Usage(input_tokens=999_950)) == "999k tok · $0.00"


def test_label_shows_what_a_task_spent_once_it_spent_anything() -> None:
    usage = Usage(cache_read_input_tokens=1_200_000, cost_usd=0.84)
    state = TaskState(id="1", task=TaskSpec(prompt="go"), status="completed", usage=usage)
    assert str(task_label(state, "1", "◠ ")) == (
        "1. ✅ go\n      default · restricted · 1.2M tok · $0.84"
    )


def test_fix_tasks_are_indented_under_their_task() -> None:
    fix = TaskState(id="2", task=TaskSpec(prompt="Fix it"), fixes="1")
    assert str(task_label(fix, "1.1", "◠ ")) == "   1.1 ⏳ Fix it\n          default · restricted"


def test_a_task_with_a_reason_shows_it_on_a_dim_red_line_under_the_details() -> None:
    state = TaskState(
        id="1", task=TaskSpec(prompt="go"), status="unverified", reason="Unit tests failed"
    )
    label = task_label(state, "1", "◠ ", width=80)
    assert str(label) == "1. ❗ go\n      default · restricted\n      ↳ Unit tests failed"
    reason_start = str(label).index("↳")
    assert any(
        span.style == "dim red" and span.start <= reason_start < span.end for span in label.spans
    )


def test_a_task_without_a_reason_keeps_its_two_lines() -> None:
    state = TaskState(id="1", task=TaskSpec(prompt="go"), status="completed")
    assert str(task_label(state, "1", "◠ ", width=80)) == "1. ✅ go\n      default · restricted"


def test_a_fix_tasks_reason_lines_up_under_its_details() -> None:
    fix = TaskState(
        id="2", task=TaskSpec(prompt="Fix it"), fixes="1", status="error", reason="boom"
    )
    assert str(task_label(fix, "1.1", "◠ ", width=80)) == (
        "   1.1 ❌ Fix it\n          default · restricted\n          ↳ boom"
    )


def test_a_reason_is_shown_whole_without_a_width() -> None:
    state = TaskState(id="1", task=TaskSpec(prompt="go"), status="unverified", reason="word " * 20)
    assert str(task_label(state, "1", "◠ ")).split("\n")[2] == f"      ↳ {'word ' * 20}"


def test_a_long_reason_is_cut_to_the_width_with_an_ellipsis() -> None:
    state = TaskState(id="1", task=TaskSpec(prompt="go"), status="unverified", reason="word " * 20)
    reason_line = str(task_label(state, "1", "◠ ", width=20)).split("\n")[2]
    assert reason_line == "      ↳ word word w…"
    assert cell_len(reason_line) == 20


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


async def test_list_numbers_its_tasks_by_id() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        one, two = board("one", "two").tasks
        fix = TaskState(id="3", task=TaskSpec(prompt="fix one"), fixes="1")
        await feed(session, pilot, TasksChanged(tasks=(one, fix, two)))
        assert app.query_one(TaskList).numbers_by_id == {"1": "1", "3": "1.1", "2": "2"}


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
        await open_task_list(pilot)  # On the running task, "two".
        await pilot.press("j")
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


async def press_enter_on_the_task(
    monkeypatch: pytest.MonkeyPatch, state: TaskState
) -> list[tuple[str, object]]:
    """The notes shown by enter on the only task, ``state``, which opens no editor."""
    session = FakeSession()
    app = OrbitApp(session)
    notes: list[tuple[str, object]] = []
    monkeypatch.setattr(app, "notify", lambda text, **kw: notes.append((text, kw["severity"])))
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings())
        await feed(session, pilot, TasksChanged(tasks=(state,)))
        await open_task_list(pilot)
        await pilot.press("enter")
        assert len(app.screen_stack) == 1
        assert session.commands == []
    return notes


async def test_enter_on_a_running_task_says_it_cant_be_edited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = TaskState(id="3", task=TaskSpec(prompt="go"), status="running")
    notes = await press_enter_on_the_task(monkeypatch, state)
    assert notes == [("Task '3' can't be edited while it runs.", "warning")]


async def test_enter_on_a_review_says_it_cant_be_edited(monkeypatch: pytest.MonkeyPatch) -> None:
    state = TaskState(id="1", task=TaskSpec(prompt="check it", role="reviewer"))
    notes = await press_enter_on_the_task(monkeypatch, state)
    assert notes == [("Reviews can't be edited; remove it instead.", "warning")]


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

        # The planner has a lane of its own, so its turns don't hold up runs.
        await feed(session, pilot, BusyChanged(busy=True, lane="planner"))
        await pilot.press("ctrl+r")
        assert session.commands == [RunTasks(), RunTasks(), RunTasks()]


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
        entries = app.query_one(Transcript).entries(None)
        # Announced by the number the list shows.
        assert [entry.text for entry in entries] == ["Task 2: two", "Task 1.1: fix one"]
        assert entries[0].kind == "user"


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
        assert session.commands == [FireTrigger(trigger=SUPER_REVIEW)]


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


async def test_a_refused_planner_prompt_frees_the_prompt() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        prompt = app.query_one(ChatPrompt)
        await pilot.press("ctrl+t", "i", *"plan", "enter")
        assert prompt.border_title == "Prompt · planner · busy"
        refused = ErrorOccurred(
            code=NOT_CONFIGURED, message="Planning needs agents and a planner role."
        )
        await feed(session, pilot, refused, BusyChanged(busy=False, lane="planner"))
        assert prompt.border_title == "Prompt · planner"


async def test_a_planner_less_session_leaves_the_prompt_usable() -> None:
    app = OrbitApp(AgentSession())
    async with app.run_test(size=(120, 30)) as pilot:
        prompt = app.query_one(ChatPrompt)
        await pilot.press("ctrl+t", "i", *"plan", "enter")
        for _ in range(5):
            await pilot.pause()
        assert prompt.border_title == "Prompt · planner"


def options(task_list: TaskList) -> list[object]:
    return [task_list.get_option_at_index(i) for i in range(task_list.option_count)]


async def test_an_unchanged_task_list_is_left_alone() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed(session, pilot, board("one", "two"))
        task_list = app.query_one(TaskList)
        before = options(task_list)
        await feed(session, pilot, board("one", "two"))
        assert options(task_list) == before


async def test_a_changed_task_relabels_its_row_in_place() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        task_list = app.query_one(TaskList)
        task_list.clock = lambda: 0.0
        await feed(session, pilot, board("one", "two"))
        before = options(task_list)
        one, two = board("one", "two").tasks
        done = one.model_copy(update={"status": "completed"})
        await feed(session, pilot, TasksChanged(tasks=(done, two)))
        assert options(task_list) == before  # The same rows, one with a new label.
        assert rows(app)[0] == str(task_label(done, "1", spinner_frame(0.0)))
        assert rows(app)[1] == str(task_label(two, "2", spinner_frame(0.0)))


async def test_tasks_coming_or_going_rebuild_the_rows() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed(session, pilot, board("one", "two"))
        task_list = app.query_one(TaskList)
        before = options(task_list)
        await feed(session, pilot, board("one", "two", "three"))
        assert len(options(task_list)) == 4
        assert not any(option in before for option in options(task_list))
        assert [row.split()[0] for row in rows(app)[:3]] == ["1.", "2.", "3."]


def cached_rows(task_list: TaskList) -> set[int]:
    """The rows whose drawing the list has cached, and so won't draw again."""
    keys = task_list._option_render_cache.keys()  # LRUCache isn't iterable itself.
    cached = {key[0] for key in keys}
    return {i for i, option in enumerate(options(task_list)) if option in cached}


async def test_spinning_and_relabelling_redraw_only_their_rows() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        task_list = app.query_one(TaskList)
        task_list.clock = lambda: 0.0
        one, two, three = board("one", "two", "three").tasks
        running = two.model_copy(update={"status": "running"})
        await feed(session, pilot, TasksChanged(tasks=(one, running, three)))
        await pilot.pause()
        before = cached_rows(task_list)
        assert {0, 1, 2} <= before
        task_list.clock = lambda: 0.25
        task_list.tick()
        assert cached_rows(task_list) == before - {1}
        await pilot.pause()
        done = three.model_copy(update={"status": "completed"})
        task_list.show((one, running, done))
        assert cached_rows(task_list) == before - {2}
        assert rows(app)[2] == str(task_label(done, "3", spinner_frame(0.25)))


async def test_task_list_ticks_only_while_a_task_runs() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        tasks = app.query_one(TaskList)
        assert not tasks.ticking
        await feed(session, pilot, board("one", "two"))
        assert not tasks.ticking
        await feed(session, pilot, board("one", "two", status="running"))
        assert tasks.ticking
        await feed(session, pilot, board("one", "two", status="verifying"))
        assert not tasks.ticking


def many(count: int, running: int | None = None) -> TasksChanged:
    """A board of ``count`` pending tasks, with task ``running`` (from 0) running if given."""
    tasks = board(*(f"task {n}" for n in range(count))).tasks
    if running is not None:
        changed = tasks[running].model_copy(update={"status": "running"})
        tasks = (*tasks[:running], changed, *tasks[running + 1 :])
    return TasksChanged(tasks=tasks)


def in_view(task_list: TaskList, index: int) -> bool:
    top, height = task_list.row_lines()[index]
    view_top = round(task_list.scroll_y)
    return view_top <= top and top + height <= view_top + task_list.scrollable_content_region.height


async def test_the_list_follows_the_active_task_down_the_board() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        task_list = app.query_one(TaskList)
        assert task_list.following
        await feed(session, pilot, many(30))
        assert task_list.scroll_y == 0  # Nothing active: it stays put.
        await feed(session, pilot, many(30, running=25))
        assert in_view(task_list, 25)
        # Scrolled just enough: the task is on the last lines.
        top, height = task_list.row_lines()[25]
        assert top + height == task_list.scroll_y + task_list.scrollable_content_region.height
        assert task_list.highlighted is None  # Following only scrolls.


async def test_the_list_scrolls_back_up_to_the_active_task_with_a_row_above_it() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        task_list = app.query_one(TaskList)
        await feed(session, pilot, many(30, running=25))
        await feed(session, pilot, many(30, running=3))
        assert task_list.scroll_y == task_list.row_lines()[2][0]


async def test_the_list_does_not_scroll_while_the_active_task_is_in_view() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        task_list = app.query_one(TaskList)
        await feed(session, pilot, many(30, running=25))
        scroll_y = task_list.scroll_y
        await feed(session, pilot, many(30, running=24))
        assert task_list.scroll_y == scroll_y


async def test_the_list_follows_the_active_task_when_resized() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 60)) as pilot:
        task_list = app.query_one(TaskList)
        await feed(session, pilot, many(30, running=12))
        assert task_list.scroll_y == 0
        await pilot.resize_terminal(120, 24)
        await pilot.pause()
        assert in_view(task_list, 12)


def wordy(count: int, running: int) -> TasksChanged:
    """A board like ``many``'s, but with titles long enough to wrap in a narrow list."""
    tasks = board(*(f"task {n} " + "word " * 30 for n in range(count))).tasks
    changed = tasks[running].model_copy(update={"status": "running"})
    return TasksChanged(tasks=(*tasks[:running], changed, *tasks[running + 1 :]))


async def test_the_list_follows_the_active_task_when_a_resize_rewraps_the_rows() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(400, 60)) as pilot:
        task_list = app.query_one(TaskList)
        await feed(session, pilot, wordy(30, running=25))
        await pilot.resize_terminal(80, 30)
        await pilot.pause()
        assert task_list.row_lines()[25][1] > 1  # The rows really did wrap.
        assert in_view(task_list, 25)


def with_reasons(
    changed: TasksChanged, *indices: int, reason: str = "Unit tests failed"
) -> TasksChanged:
    """``changed`` with the tasks at ``indices`` ended unverified for ``reason``."""
    tasks = list(changed.tasks)
    for i in indices:
        tasks[i] = tasks[i].model_copy(update={"status": "unverified", "reason": reason})
    return TasksChanged(tasks=tuple(tasks))


async def test_the_list_follows_the_active_task_below_rows_with_a_reason() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        task_list = app.query_one(TaskList)
        await feed(session, pilot, with_reasons(many(30, running=25), *range(25)))
        heights = [height for _, height in task_list.row_lines()]
        assert heights[24] == heights[26] + 1  # The reason's line.
        assert in_view(task_list, 25)
        top, height = task_list.row_lines()[25]
        assert top + height == task_list.scroll_y + task_list.scrollable_content_region.height


async def test_the_list_follows_the_active_task_as_reasons_come_and_go_above_it() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        task_list = app.query_one(TaskList)
        await feed(session, pilot, many(30, running=25))
        await feed(session, pilot, with_reasons(many(30, running=25), *range(25)))
        await pilot.pause()
        assert in_view(task_list, 25)
        await feed(session, pilot, many(30, running=25))
        await pilot.pause()
        heights = [height for _, height in task_list.row_lines()]
        assert heights[24] == heights[26]
        assert in_view(task_list, 25)


async def test_a_long_reason_is_cut_to_the_list_rather_than_wrapped() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(400, 30)) as pilot:
        task_list = app.query_one(TaskList)
        await feed(session, pilot, with_reasons(many(2), 0, reason="word " * 200))
        for size in ((400, 30), (80, 30)):
            await pilot.resize_terminal(*size)
            await pilot.pause()
            heights = [height for _, height in task_list.row_lines()]
            assert heights[0] == heights[1] + 1  # The reason takes one line, however long.
            assert rows(app)[0].endswith("…")


async def test_following_leaves_the_highlight_alone() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        task_list = app.query_one(TaskList)
        await feed(session, pilot, many(30, running=1))
        await open_task_list(pilot)
        await pilot.press("k", "escape", "escape")
        assert task_list.following
        assert task_list.highlighted == 0
        await feed(session, pilot, many(30, running=25))
        assert task_list.scroll_y > 0
        assert in_view(task_list, 25)
        assert task_list.highlighted == 0


async def test_activating_the_list_highlights_the_active_task() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        task_list = app.query_one(TaskList)
        await feed(session, pilot, many(30, running=25))
        await open_task_list(pilot)
        assert task_list.highlighted == 25
        assert in_view(task_list, 25)
        # It lands there even when the highlight was elsewhere.
        await pilot.press("k", "k", "escape", "enter")
        assert task_list.highlighted == 25


def test_a_list_not_laid_out_yet_has_nothing_to_follow() -> None:
    task_list = TaskList(id="task-list")
    verifying = board("one", status="verifying").tasks
    task_list.show(verifying)
    assert task_list.row_lines() == []
    assert task_list.scroll_y == 0


def title(task_list: TaskList) -> str:
    return str(task_list.border_title)


def wheel(task_list: TaskList, down: bool = True) -> None:
    """Scroll ``task_list`` one notch with the mouse wheel."""
    kind = events.MouseScrollDown if down else events.MouseScrollUp
    task_list.post_message(kind(task_list, 0, 0, 0, 1 if down else -1, 0, False, False, False))


async def test_activating_the_list_holds_it() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        task_list = app.query_one(TaskList)
        assert title(task_list) == "Task list"
        await open_task_list(pilot)
        assert not task_list.following
        assert title(task_list) == "Task list · held"


async def test_the_first_escape_leaves_list_mode_still_held() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        task_list = app.query_one(TaskList)
        await feed(session, pilot, many(30, running=25))
        await open_task_list(pilot)
        await pilot.press("k", "escape")
        assert app.focused is None
        assert not task_list.following
        assert title(task_list) == "Task list · held"
        assert task_list.highlighted == 24


async def test_escape_again_lets_go_and_scrolls_to_the_active_task() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        task_list = app.query_one(TaskList)
        await feed(session, pilot, many(30, running=25))
        await open_task_list(pilot)
        await pilot.press(*"k" * 25, "escape")
        assert task_list.scroll_y == 0
        await pilot.press("escape")
        assert task_list.following
        assert title(task_list) == "Task list"
        assert in_view(task_list, 25)
        assert task_list.highlighted == 0  # Letting go only scrolls.


async def test_the_mouse_wheel_holds_the_list() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        task_list = app.query_one(TaskList)
        await feed(session, pilot, many(30))
        wheel(task_list)
        await pilot.pause()
        assert not task_list.following
        assert title(task_list) == "Task list · held"
        await pilot.press("l", "k", "escape")
        assert task_list.following
        assert title(task_list) == "Task list"
        assert selected(app) == ["task-list"]
        wheel(task_list, down=False)
        await pilot.pause()
        assert not task_list.following
        assert title(task_list) == "Task list · held"


async def test_following_scrolls_do_not_hold_the_list() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        task_list = app.query_one(TaskList)
        await feed(session, pilot, many(30, running=25))
        await feed(session, pilot, many(30, running=3))
        assert task_list.scroll_y > 0
        assert task_list.following
        assert title(task_list) == "Task list"


async def test_a_held_list_does_not_scroll_as_the_board_changes() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        task_list = app.query_one(TaskList)
        await feed(session, pilot, many(30))
        await open_task_list(pilot)
        await pilot.press("escape")
        await feed(session, pilot, many(30, running=25))
        assert task_list.scroll_y == 0
        await pilot.resize_terminal(100, 20)
        await pilot.pause()
        assert task_list.scroll_y == 0


async def test_escape_on_a_list_not_held_is_harmless() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        task_list = app.query_one(TaskList)
        await feed(session, pilot, many(30, running=25))
        scroll_y = task_list.scroll_y
        await pilot.press("l", "k", "escape")
        assert selected(app) == ["task-list"]
        assert task_list.following
        assert task_list.scroll_y == scroll_y
        assert title(task_list) == "Task list"


async def test_opening_a_session_lets_go_of_the_list() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        task_list = app.query_one(TaskList)
        await feed(session, pilot, many(30))
        await open_task_list(pilot)
        await pilot.press("escape")
        snapshot = opened(SessionSummary(id="s", name="s"))
        await feed(session, pilot, snapshot, many(30, running=25))
        assert task_list.following
        assert title(task_list) == "Task list"
        assert in_view(task_list, 25)


async def test_opening_a_session_while_the_list_is_active_leaves_list_mode_and_lets_go() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        task_list = app.query_one(TaskList)
        await feed(session, pilot, many(30))
        await open_task_list(pilot)
        snapshot = opened(SessionSummary(id="s", name="s"))
        await feed(session, pilot, snapshot, many(30, running=25))
        assert app.focused is None
        assert task_list.following
        assert title(task_list) == "Task list"
        assert in_view(task_list, 25)


async def test_switching_session_through_ctrl_p_while_the_list_is_active_leaves_list_mode() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        task_list = app.query_one(TaskList)
        await feed(session, pilot, many(30))
        await open_task_list(pilot)
        await pilot.press("ctrl+p")
        other = SessionSummary(id="old", name="old")
        await feed(session, pilot, SessionsListed(sessions=(other,), reason="asked"))
        await pilot.press("j", "enter")
        await feed(session, pilot, opened(other), many(30, running=25))
        assert session.commands[-1] == OpenSession(session_id="old")
        assert not isinstance(app.screen, SessionsScreen)
        assert app.focused is None
        assert task_list.following
        assert title(task_list) == "Task list"
        assert in_view(task_list, 25)


async def test_opening_a_session_replaces_the_total_with_its_saved_spend() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        task_list = app.query_one(TaskList)
        spent = Usage(input_tokens=1_000, cost_usd=0.84)
        await feed(session, pilot, TurnFinished(turn_id="a", reason="completed", usage=spent))
        saved = Usage(input_tokens=2_000, cost_usd=2.0)
        await feed(session, pilot, opened(SessionSummary(id="b", name="b", usage=saved)))
        assert task_list.border_subtitle == "session: 2k tok · $2.00"


async def test_a_turn_after_opening_a_session_adds_to_its_total() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        task_list = app.query_one(TaskList)
        await feed(
            session,
            pilot,
            TurnFinished(turn_id="a", reason="completed", usage=Usage(cost_usd=0.84)),
            opened(SessionSummary(id="b", name="b", usage=Usage(cost_usd=2.0))),
            TurnFinished(turn_id="b", reason="completed", usage=Usage(cost_usd=0.5)),
        )
        assert task_list.border_subtitle == "session: 0 tok · $2.50"


async def test_a_turn_that_spent_nothing_shows_no_total() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        free = TurnFinished(turn_id="a", reason="completed", usage=Usage())
        await feed(session, pilot, free)
        assert not app.query_one(TaskList).border_subtitle


async def test_counting_and_resetting_to_the_same_total_show_the_same() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test():
        task_list = app.query_one(TaskList)
        total = Usage(input_tokens=1_000, cost_usd=0.84)
        task_list.count(total)
        counted = task_list.border_subtitle
        task_list.reset_usage(total)
        assert task_list.border_subtitle == counted == "session: 1k tok · $0.84"
        task_list.reset_usage(Usage())
        assert not task_list.border_subtitle


async def test_opening_a_new_session_clears_the_total() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        task_list = app.query_one(TaskList)
        spent = Usage(input_tokens=1_000, cost_usd=0.84)
        await feed(session, pilot, TurnFinished(turn_id="a", reason="completed", usage=spent))
        await feed(session, pilot, opened(SessionSummary(id="n", name="n")))
        assert not task_list.border_subtitle
        assert task_list.session_usage == Usage()
