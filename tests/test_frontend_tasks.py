"""The task list as the frontend follows it, without Textual: the board and totals it shows, the
tasks announced, the wait for a coder, and what running, reviewing and editing send."""

from orbit.protocol import (
    NOT_CONFIGURED,
    SUPER_REVIEW,
    Activity,
    AddTask,
    AgentEntry,
    BusyChanged,
    EditTask,
    ErrorOccurred,
    FireTrigger,
    RunTasks,
    SessionStateChanged,
    SessionSummary,
    Settings,
    SettingsChanged,
    TasksChanged,
    TaskSpec,
    TaskStarted,
    TaskState,
    TurnFinished,
    Usage,
)
from orbit.tui.view.task_editor import TaskChoices
from tests.frontend_fakes import Shell
from tests.session_apps import CATALOG
from tests.tui_fakes import board, opened


async def test_the_list_shows_the_board_the_backend_reports() -> None:
    shell = Shell()
    one, two = board("one", "two", status="completed").tasks
    fix = TaskState(id="3", task=TaskSpec(prompt="fix one"), fixes="1")
    await shell.feed(TasksChanged(tasks=(one, fix, two)))
    assert shell.task_view.rows == [
        "1. ✅ one\n      default · restricted",
        "   1.1 ⏳ fix one\n          default · restricted",
        "2. ✅ two\n      default · restricted",
        "+ new task",
    ]


async def test_a_started_task_is_announced_by_its_number_in_the_list() -> None:
    shell = Shell()
    one, two = board("one", "two").tasks
    fix = TaskState(id="3", task=TaskSpec(prompt="fix one"), fixes="1")
    await shell.feed(
        TasksChanged(tasks=(one, fix, two)), TaskStarted(task_id="2"), TaskStarted(task_id="3")
    )
    assert shell.entries() == [("Task 2: two", "user"), ("Task 1.1: fix one", "user")]


async def test_running_tasks_is_asked_for_unless_the_work_lane_is_busy() -> None:
    shell = Shell()
    await shell.frontend.run_tasks()
    assert shell.wire.commands == [RunTasks()]
    await shell.feed(BusyChanged(busy=True))
    await shell.frontend.run_tasks()
    assert shell.wire.commands == [RunTasks()]
    await shell.feed(BusyChanged(busy=False))
    await shell.frontend.run_tasks()
    assert shell.wire.commands == [RunTasks(), RunTasks()]
    # The planner has a lane of its own, so its turns don't hold up runs.
    await shell.feed(BusyChanged(busy=True, lane="planner"))
    await shell.frontend.run_tasks()
    assert shell.wire.commands == [RunTasks(), RunTasks(), RunTasks()]


async def test_a_super_review_is_asked_for_even_while_busy() -> None:
    shell = Shell()
    await shell.feed(BusyChanged(busy=True))
    await shell.frontend.super_review()
    assert shell.wire.commands == [FireTrigger(trigger=SUPER_REVIEW)]


async def test_the_list_shows_each_tasks_usage_and_the_sessions_total() -> None:
    shell = Shell()
    assert shell.task_view.usage == ""
    spent = TaskState(
        id="1", task=TaskSpec(prompt="one"), usage=Usage(input_tokens=1_500, cost_usd=0.2)
    )
    await shell.feed(TasksChanged(tasks=(spent,)))
    assert shell.task_view.rows[0].endswith("default · restricted · 1.5k tok · $0.20")
    # Every metered turn counts towards the session, whether or not a task ran it.
    await shell.feed(
        TurnFinished(turn_id="a", reason="completed", usage=Usage(input_tokens=1_000)),
        TurnFinished(turn_id="b", reason="completed"),
        TurnFinished(
            turn_id="c",
            reason="error",
            agent_id="agent-1",
            usage=Usage(output_tokens=500, cost_usd=0.3),
        ),
    )
    assert shell.task_view.usage == "session: 1.5k tok · $0.30"


async def test_opening_a_session_replaces_the_total_with_its_saved_spend() -> None:
    shell = Shell()
    spent = Usage(input_tokens=1_000, cost_usd=0.84)
    await shell.feed(TurnFinished(turn_id="a", reason="completed", usage=spent))
    saved = Usage(input_tokens=2_000, cost_usd=2.0)
    await shell.feed(opened(SessionSummary(id="b", name="b", usage=saved)))
    assert shell.task_view.usage == "session: 2k tok · $2.00"


async def test_a_refused_planner_prompt_frees_the_prompt() -> None:
    shell = Shell()
    await shell.toggle_planner()
    await shell.submit("plan")
    assert shell.marks.title == "Prompt · planner · busy"
    refused = ErrorOccurred(
        code=NOT_CONFIGURED, message="Planning needs agents and a planner role."
    )
    await shell.feed(refused, BusyChanged(busy=False, lane="planner"))
    assert shell.marks.title == "Prompt · planner"


def waiting_state(*activity: Activity) -> SessionStateChanged:
    """The active session ``now`` running ``activity``."""
    return SessionStateChanged(
        session="now", active=True, state="running", viewers=1, activity=activity
    )


async def test_the_next_task_shows_the_wait_for_a_coder_until_it_starts() -> None:
    shell = Shell()
    tasks = TasksChanged(
        tasks=tuple(TaskState(id=f"t{n}", task=TaskSpec(prompt=f"task {n}")) for n in (1, 2))
    )
    run = Activity(kind="task_run", task_id="t1")
    wait = Activity(kind="waiting_for_coder", position=1, busy=2)
    await shell.feed(CATALOG, tasks, waiting_state(run, wait))
    assert shell.task_view.rows[:2] == [
        "1. ⏳ task 1\n      default · restricted · ⏳ waiting: 2 coders busy",
        "2. ⏳ task 2\n      default · restricted",
    ]
    await shell.feed(waiting_state(run))
    assert shell.task_view.rows[0] == "1. ⏳ task 1\n      default · restricted"


async def test_a_running_task_cant_be_edited_and_the_user_is_told() -> None:
    shell = Shell()
    running = TaskState(id="3", task=TaskSpec(prompt="go"), status="running")
    shell.frontend.edit_task(running)
    assert shell.app.warnings == ["Task '3' can't be edited while it runs."]
    assert shell.app.editors == []


async def test_the_editor_opens_with_the_settings_shown_and_its_task_is_saved() -> None:
    shell = Shell()
    settings = Settings(model="opus", tool_access="off")
    await shell.feed(SettingsChanged(settings=settings, model_choices=()))
    pending = TaskState(id="2", task=TaskSpec(prompt="go"))
    shell.frontend.edit_task(None)
    shell.frontend.edit_task(pending)
    [(new, new_choices, add), (old, _, edit)] = shell.app.editors
    assert (new, new_choices, old) == (None, TaskChoices(settings), pending.task)
    await add(TaskSpec(prompt="new"))
    await edit(TaskSpec(prompt="changed"))
    await edit(None)
    assert shell.wire.commands == [
        AddTask(task=TaskSpec(prompt="new")),
        EditTask(task_id="2", task=TaskSpec(prompt="changed")),
    ]


async def test_the_editor_opens_with_the_default_settings_before_any_are_reported() -> None:
    shell = Shell()
    shell.frontend.edit_task(None)
    [(_, choices, _)] = shell.app.editors
    assert choices == TaskChoices(Settings())


async def test_the_editor_offers_the_agents_of_the_latest_catalog() -> None:
    shell = Shell()
    security = AgentEntry(name="security", label="security", conversational=False)
    await shell.feed(CATALOG.model_copy(update={"agents": (security,)}))
    shell.frontend.edit_task(None)
    [(_, choices, _)] = shell.app.editors
    assert choices.agents == (security,)
