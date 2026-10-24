"""The task list's board without the widget showing it: its rows and numbers, its spinners,
following the active task, holding and letting go, the tasks the user picks, the session's
total and the wait for a coder."""

from collections.abc import Sequence

from rich.cells import cell_len
from rich.text import Text

from orbit.protocol import TaskSpec, TaskState, TaskStatus, Usage
from orbit.tui.view.common import kept_highlight, spinner_frame
from orbit.tui.view.tasks import TaskBoard, task_label


class View:
    """What the task list shows, as it shows it, and what the user has done in it: the row
    highlighted, which stays on its task's row as the rows change as a list pane keeps it, and
    the lines in view, a line for each line of a row's label."""

    def __init__(self, height: int = 10) -> None:
        self.highlighted: int | None = None
        self.row_width = 60
        self.scroll_top = 0
        self.view_height = height
        self.measured = True
        self.title = ""
        self.usage = ""
        self.keys: list[str | None] = []
        self.labels: list[Text] = []
        self.redraws = 0
        self.scrolls: list[int] = []
        self.ticking = False
        self.edits: list[TaskState | None] = []
        self.accepted: list[str] = []
        self.removed: list[str] = []
        self.whys: list[tuple[TaskState, str]] = []

    @property
    def rows(self) -> list[str]:
        return [str(label) for label in self.labels]

    def row_lines(self) -> list[tuple[int, int]]:
        lines: list[tuple[int, int]] = []
        top = 0
        for label in self.labels if self.measured else ():
            height = len(str(label).split("\n"))
            lines.append((top, height))
            top += height
        return lines

    def show_title(self, title: str) -> None:
        self.title = title

    def show_usage(self, usage: str) -> None:
        self.usage = usage

    def show_labels(self, rows: Sequence[tuple[str, Text]], trailing: Text) -> None:
        keys: list[str | None] = [key for key, _ in rows]
        keys.append(None)
        if keys != self.keys:
            self.highlighted = kept_highlight(self.keys, self.highlighted, keys)
        self.keys, self.labels = keys, [*(label for _, label in rows), trailing]
        self.redraws += 1

    def highlight(self, index: int) -> None:
        self.highlighted = index

    def scroll_to_line(self, top: int) -> None:
        self.scroll_top = top
        self.scrolls.append(top)

    def keep_ticking(self, running: bool) -> None:
        self.ticking = running

    def edit_task(self, task: TaskState | None) -> None:
        self.edits.append(task)

    def accept_task(self, task_id: str) -> None:
        self.accepted.append(task_id)

    def remove_task(self, task_id: str) -> None:
        self.removed.append(task_id)

    def show_why(self, state: TaskState, number: str) -> None:
        self.whys.append((state, number))


def board(*prompts: str, status: TaskStatus = "pending") -> tuple[TaskState, ...]:
    """Tasks with ids "1", "2", … and the given prompts."""
    return tuple(
        TaskState(id=str(i), task=TaskSpec(prompt=p), status=status)
        for i, p in enumerate(prompts, 1)
    )


def started(height: int = 10, now: float = 0.0) -> tuple[TaskBoard, View]:
    """A board started in a view ``height`` lines high, its clock stopped at ``now``."""
    view = View(height)
    tasks = TaskBoard(view, lambda: now)
    tasks.start()
    return tasks, view


def activate(tasks: TaskBoard, view: View) -> None:
    """Activate the list as list mode does: held, the first row highlighted if none is, then
    the active task, if there is one."""
    tasks.hold()
    if view.highlighted is None:
        view.highlighted = 0
    tasks.activated(True)


def many(count: int, running: int | None = None) -> tuple[TaskState, ...]:
    """A board of ``count`` pending tasks, with task ``running`` (from 0) running if given."""
    tasks = board(*(f"task {n}" for n in range(count)))
    if running is None:
        return tasks
    changed = tasks[running].model_copy(update={"status": "running"})
    return (*tasks[:running], changed, *tasks[running + 1 :])


def with_reasons(
    tasks: tuple[TaskState, ...], *indices: int, reason: str = "Unit tests failed"
) -> tuple[TaskState, ...]:
    """``tasks`` with those at ``indices`` ended unverified for ``reason``."""
    changed = list(tasks)
    for i in indices:
        changed[i] = changed[i].model_copy(update={"status": "unverified", "reason": reason})
    return tuple(changed)


def in_view(view: View, index: int) -> bool:
    top, height = view.row_lines()[index]
    return view.scroll_top <= top and top + height <= view.scroll_top + view.view_height


def at_bottom(view: View, index: int) -> bool:
    """Whether row ``index`` is on the last lines in view."""
    top, height = view.row_lines()[index]
    return top + height == view.scroll_top + view.view_height


def heights(view: View) -> list[int]:
    return [height for _, height in view.row_lines()]


def test_starting_shows_only_the_new_task_row_under_its_title() -> None:
    tasks, view = started()
    assert view.rows == ["+ new task"]
    assert view.labels[-1].style == "dim italic"
    assert view.title == "Task list" and tasks.following
    assert not view.usage  # Nothing spent yet.


def test_the_board_shows_each_task_numbered_with_fixes_under_their_task() -> None:
    tasks, view = started()
    one, two = board("one", "two", status="completed")
    fix = TaskState(id="3", task=TaskSpec(prompt="fix one"), fixes="1")
    tasks.show((one, fix, two))
    assert view.rows == [
        "1. ✅ one\n      default · restricted",
        "   1.1 ⏳ fix one\n          default · restricted",
        "2. ✅ two\n      default · restricted",
        "+ new task",
    ]
    assert view.keys == ["1", "3", "2", None]


def test_the_board_numbers_its_tasks_by_id() -> None:
    tasks, _ = started()
    one, two = board("one", "two")
    fix = TaskState(id="3", task=TaskSpec(prompt="fix one"), fixes="1")
    tasks.show((one, fix, two))
    assert tasks.numbers_by_id == {"1": "1", "3": "1.1", "2": "2"}
    assert tasks.numbers == ["1", "1.1", "2"]
    assert tasks.tasks == (one, fix, two)


def test_running_tasks_spin_in_place_and_nothing_spins_once_nothing_runs() -> None:
    now = [0.0]
    view = View()
    tasks = TaskBoard(view, lambda: now[0])
    tasks.start()
    one, two, three = board("one", "two", "three")
    tasks.show(
        (
            one.model_copy(update={"status": "completed"}),
            two.model_copy(update={"status": "running"}),
            three.model_copy(update={"status": "verifying"}),
        )
    )
    activate(tasks, view)
    assert view.highlighted == 1  # On the running task, "two".
    view.highlighted = 2
    assert [r.split("\n")[0] for r in view.rows[:3]] == ["1. ✅ one", "2. ◜  two", "3. 🔍 three"]

    keys = view.keys
    now[0] = 0.25  # Two frames on, at 8 a second.
    tasks.redraw()
    assert view.rows[1].startswith("2. ◝  two")
    assert view.rows[2].startswith("3. 🔍 three")  # Only running tasks spin.
    assert view.keys == keys and view.highlighted == 2  # Relabelled in place.

    tasks.show(board("one", "two", "three", status="completed"))
    before = view.rows
    now[0] = 0.5
    tasks.redraw()
    assert view.rows == before


def test_the_highlight_stays_on_its_task_as_the_board_changes() -> None:
    tasks, view = started()
    one, two = board("one", "two")
    tasks.show((one, two))
    activate(tasks, view)
    view.highlighted = 1  # On "two".

    # A fix task appears before it: the highlight follows "two" down.
    fix = TaskState(id="9", task=TaskSpec(prompt="fix"))
    tasks.show((one, fix, two))
    assert view.highlighted == 2

    # On the new-task row, it stays there as tasks are added.
    view.highlighted = 3
    tasks.show((one, fix, two, TaskState(id="10", task=TaskSpec(prompt="three"))))
    assert view.highlighted == 4

    # Activating the list again keeps the highlight where it was.
    activate(tasks, view)
    assert view.highlighted == 4


def test_removing_the_only_task_leaves_nothing_to_select() -> None:
    tasks, view = started()
    tasks.show(board("one"))
    activate(tasks, view)
    assert view.highlighted == 0
    tasks.show(())
    assert view.highlighted is None
    tasks.select()
    assert view.edits == []


def test_select_asks_to_edit_the_task_highlighted_or_a_new_one() -> None:
    tasks, view = started()
    one, two = board("one", "two")
    tasks.show((one, two))
    view.highlighted = 1
    tasks.select()
    view.highlighted = 2
    tasks.select()
    assert view.edits == [two, None]


def test_a_accepts_the_highlighted_draft_and_x_removes_a_task() -> None:
    tasks, view = started()
    draft = TaskState(id="1", task=TaskSpec(prompt="plan step"), status="draft")
    queued = TaskState(id="2", task=TaskSpec(prompt="queued"))
    tasks.show((draft, queued))
    activate(tasks, view)
    tasks.accept()
    view.highlighted = 1
    tasks.accept()  # Not a draft: nothing to accept.
    tasks.remove()
    view.highlighted = 2  # The new-task row.
    tasks.accept()
    tasks.remove()
    assert (view.accepted, view.removed) == (["1"], ["2"])


def test_why_a_task_ended_shows_for_a_task_with_a_reason_by_its_number() -> None:
    tasks, view = started()
    one, two = board("one", "two")
    failed = two.model_copy(update={"status": "error", "reason": "boom"})
    fix = TaskState(id="3", task=TaskSpec(prompt="fix one"), fixes="1", reason="gave up")
    tasks.show((one, fix, failed))
    for row in range(4):
        view.highlighted = row
        tasks.explain()
    assert view.whys == [(fix, "1.1"), (failed, "2")]


def test_nothing_highlighted_picks_no_task() -> None:
    tasks, view = started()
    tasks.show(board("one"))
    tasks.select()
    tasks.accept()
    tasks.remove()
    tasks.explain()
    assert (view.edits, view.accepted, view.removed, view.whys) == ([], [], [], [])
    assert tasks.highlighted_task is None


def test_an_unchanged_board_is_left_alone() -> None:
    tasks, view = started()
    tasks.show(board("one", "two"))
    redraws = view.redraws
    tasks.show(board("one", "two"))
    assert view.redraws == redraws


def test_a_changed_task_relabels_its_row_in_place() -> None:
    tasks, view = started()
    one, two = board("one", "two")
    tasks.show((one, two))
    keys = view.keys
    done = one.model_copy(update={"status": "completed"})
    tasks.show((done, two))
    assert view.keys == keys  # The same rows, one with a new label.
    assert view.rows[0] == str(task_label(done, "1", spinner_frame(0.0)))
    assert view.rows[1] == str(task_label(two, "2", spinner_frame(0.0)))


def test_tasks_coming_or_going_change_the_rows_keys() -> None:
    tasks, view = started()
    tasks.show(board("one", "two"))
    tasks.show(board("one", "two", "three"))
    assert view.keys == ["1", "2", "3", None]
    assert [row.split()[0] for row in view.rows[:3]] == ["1.", "2.", "3."]


def test_only_a_running_task_relabels_as_the_spinners_turn() -> None:
    now = [0.0]
    view = View()
    tasks = TaskBoard(view, lambda: now[0])
    one, two, three = board("one", "two", "three")
    running = two.model_copy(update={"status": "running"})
    tasks.show((one, running, three))
    before = view.rows
    now[0] = 0.25
    tasks.redraw()
    assert [i for i, row in enumerate(view.rows) if row != before[i]] == [1]
    done = three.model_copy(update={"status": "completed"})
    spun = view.rows
    tasks.show((one, running, done))
    assert [i for i, row in enumerate(view.rows) if row != spun[i]] == [2]
    assert view.rows[2] == str(task_label(done, "3", spinner_frame(0.25)))


def test_the_spinners_turn_only_while_a_task_runs() -> None:
    tasks, view = started()
    assert not view.ticking
    tasks.show(board("one", "two"))
    assert not view.ticking
    tasks.show(board("one", "two", status="running"))
    assert view.ticking
    tasks.show(board("one", "two", status="verifying"))
    assert not view.ticking


def test_the_board_follows_the_active_task_down_the_board() -> None:
    tasks, view = started()
    assert tasks.following
    tasks.show(many(30))
    assert view.scroll_top == 0 and view.scrolls == []  # Nothing active: it stays put.
    tasks.show(many(30, running=25))
    assert in_view(view, 25)
    assert at_bottom(view, 25)  # Scrolled just enough: the task is on the last lines.
    assert view.highlighted is None  # Following only scrolls.


def test_the_board_scrolls_back_up_to_the_active_task_with_a_row_above_it() -> None:
    tasks, view = started()
    tasks.show(many(30, running=25))
    tasks.show(many(30, running=3))
    assert view.scroll_top == view.row_lines()[2][0]


def test_the_board_does_not_scroll_while_the_active_task_is_in_view() -> None:
    tasks, view = started()
    tasks.show(many(30, running=25))
    scrolls = list(view.scrolls)
    tasks.show(many(30, running=24))
    assert view.scrolls == scrolls


def test_fitting_a_smaller_view_follows_the_active_task() -> None:
    tasks, view = started(height=60)
    tasks.show(many(30, running=12))
    assert view.scroll_top == 0
    view.view_height = 10
    tasks.redraw()
    tasks.follow()
    assert in_view(view, 12)


def test_the_board_follows_the_active_task_below_rows_with_a_reason() -> None:
    tasks, view = started()
    tasks.show(with_reasons(many(30, running=25), *range(25)))
    assert heights(view)[24] == heights(view)[26] + 1  # The reason's line.
    assert in_view(view, 25) and at_bottom(view, 25)


def test_the_board_follows_the_active_task_as_reasons_come_and_go_above_it() -> None:
    tasks, view = started()
    tasks.show(many(30, running=25))
    tasks.show(with_reasons(many(30, running=25), *range(25)))
    assert in_view(view, 25)
    tasks.show(many(30, running=25))
    assert heights(view)[24] == heights(view)[26]
    assert in_view(view, 25)


def test_a_long_reason_is_cut_to_the_rows_width() -> None:
    tasks, view = started()
    view.row_width = 400
    tasks.show(with_reasons(many(2), 0, reason="word " * 200))
    for width in (400, 80):
        view.row_width = width
        tasks.redraw()
        reason_line = view.rows[0].split("\n")[-1]
        assert reason_line.endswith("…") and cell_len(reason_line) == width
        assert heights(view)[0] == heights(view)[1] + 1  # The reason takes one line.


def test_following_leaves_the_highlight_alone() -> None:
    tasks, view = started()
    tasks.show(many(30, running=1))
    activate(tasks, view)
    view.highlighted = 0
    tasks.let_go()
    assert tasks.following
    assert view.highlighted == 0
    tasks.show(many(30, running=25))
    assert view.scroll_top > 0
    assert in_view(view, 25)
    assert view.highlighted == 0


def test_activating_the_list_highlights_the_active_task() -> None:
    tasks, view = started()
    tasks.show(many(30, running=25))
    activate(tasks, view)
    assert view.highlighted == 25
    assert in_view(view, 25)
    # It lands there even when the highlight was elsewhere.
    view.highlighted = 23
    activate(tasks, view)
    assert view.highlighted == 25


def test_a_list_that_took_no_focus_highlights_nothing() -> None:
    tasks, view = started()
    tasks.show(many(30, running=25))
    tasks.hold()
    tasks.activated(False)
    assert view.highlighted is None
    assert view.title == "Task list · held"


def test_a_board_not_measured_yet_has_nothing_to_follow() -> None:
    view = View()
    view.measured = False
    tasks = TaskBoard(view, lambda: 0.0)
    tasks.show(board("one", status="verifying"))
    assert view.scrolls == []


def test_activating_the_list_holds_it() -> None:
    tasks, view = started()
    assert view.title == "Task list"
    activate(tasks, view)
    assert not tasks.following
    assert view.title == "Task list · held"


def test_a_held_list_stays_held_and_highlighted_until_let_go() -> None:
    tasks, view = started()
    tasks.show(many(30, running=25))
    activate(tasks, view)
    view.highlighted = 24
    assert not tasks.following
    assert view.title == "Task list · held"
    assert view.highlighted == 24


def test_letting_go_follows_again_and_scrolls_to_the_active_task() -> None:
    tasks, view = started()
    tasks.show(many(30, running=25))
    activate(tasks, view)
    view.highlighted, view.scroll_top = 0, 0  # The list scrolled to the highlight.
    tasks.follow()
    assert view.scroll_top == 0
    tasks.let_go()
    assert tasks.following
    assert view.title == "Task list"
    assert in_view(view, 25)
    assert view.highlighted == 0  # Letting go only scrolls.


def test_holding_and_letting_go_again() -> None:
    tasks, view = started()
    tasks.show(many(30))
    tasks.hold()
    assert not tasks.following
    assert view.title == "Task list · held"
    tasks.let_go()
    assert tasks.following
    assert view.title == "Task list"
    tasks.hold()
    assert not tasks.following
    assert view.title == "Task list · held"


def test_following_scrolls_do_not_hold_the_list() -> None:
    tasks, view = started()
    tasks.show(many(30, running=25))
    tasks.show(many(30, running=3))
    assert view.scroll_top > 0
    assert tasks.following
    assert view.title == "Task list"


def test_a_held_list_does_not_scroll_as_the_board_changes_or_fits() -> None:
    tasks, view = started()
    tasks.show(many(30))
    activate(tasks, view)
    tasks.show(many(30, running=25))
    assert view.scroll_top == 0
    view.view_height = 8
    tasks.redraw()
    tasks.follow()
    assert view.scroll_top == 0 and view.scrolls == []


def test_letting_go_of_a_list_not_held_is_harmless() -> None:
    tasks, view = started()
    tasks.show(many(30, running=25))
    scrolls, title = list(view.scrolls), view.title
    tasks.let_go()
    assert tasks.following
    assert view.scrolls == scrolls
    assert view.title == title == "Task list"


def test_each_turn_counts_towards_the_sessions_total() -> None:
    tasks, view = started()
    spent = TaskState(
        id="1", task=TaskSpec(prompt="one"), usage=Usage(input_tokens=1_500, cost_usd=0.2)
    )
    tasks.show((spent,))
    assert view.rows[0].endswith("default · restricted · 1.5k tok · $0.20")
    tasks.count(Usage(input_tokens=1_000))
    tasks.count(Usage(output_tokens=500, cost_usd=0.3))
    assert view.usage == "session: 1.5k tok · $0.30"


def test_a_saved_spend_replaces_the_total_and_turns_add_to_it() -> None:
    tasks, view = started()
    tasks.count(Usage(input_tokens=1_000, cost_usd=0.84))
    tasks.reset_usage(Usage(input_tokens=2_000, cost_usd=2.0))
    assert view.usage == "session: 2k tok · $2.00"
    tasks.reset_usage(Usage(cost_usd=2.0))
    tasks.count(Usage(cost_usd=0.5))
    assert view.usage == "session: 0 tok · $2.50"


def test_a_turn_that_spent_nothing_shows_no_total() -> None:
    tasks, view = started()
    tasks.count(Usage())
    assert not view.usage


def test_counting_and_resetting_to_the_same_total_show_the_same() -> None:
    tasks, view = started()
    total = Usage(input_tokens=1_000, cost_usd=0.84)
    tasks.count(total)
    counted = view.usage
    tasks.reset_usage(total)
    assert view.usage == counted == "session: 1k tok · $0.84"
    tasks.reset_usage(Usage())
    assert not view.usage


def test_a_new_session_clears_the_total() -> None:
    tasks, view = started()
    tasks.count(Usage(input_tokens=1_000, cost_usd=0.84))
    tasks.reset_usage(Usage())
    assert not view.usage
    assert tasks.session_usage == Usage()


def test_the_next_task_shows_the_wait_for_a_coder_until_it_stops_waiting() -> None:
    tasks, view = started()
    tasks.show(tuple(TaskState(id=f"t{n}", task=TaskSpec(prompt=f"task {n}")) for n in (1, 2)))
    tasks.show_waiting(("t1", "⏳ waiting: 2 coders busy"))
    assert view.rows[:2] == [
        "1. ⏳ task 1\n      default · restricted · ⏳ waiting: 2 coders busy",
        "2. ⏳ task 2\n      default · restricted",
    ]
    redraws = view.redraws
    tasks.show_waiting(("t1", "⏳ waiting: 2 coders busy"))
    assert view.redraws == redraws
    tasks.show_waiting(None)
    assert view.rows[0] == "1. ⏳ task 1\n      default · restricted"


def test_find_gives_the_task_with_an_id_on_the_list() -> None:
    tasks, _ = started()
    one, two = board("one", "two")
    tasks.show((one, two))
    assert (tasks.find("2"), tasks.find("3")) == (two, None)
