"""The task list."""

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from rich.cells import cell_len
from rich.text import Text

from orbit.protocol import (
    ACTIVE_STATUSES,
    WORK_LANE,
    AgentEntry,
    MergeHold,
    TaskState,
    Usage,
    given_skills,
)
from orbit.protocol import edit_refusal as shared_edit_refusal
from orbit.tui.view.common import STATUS_ICONS, spinner_frame
from orbit.tui.view.detail_views import ERROR
from orbit.tui.view.settings import settings_fields
from orbit.tui.view.task_editor import skills_text
from orbit.tui.view.usage import usage_label

NEW_TASK_LABEL = "+ new task"
USAGE_UNKNOWN = "usage unknown"
WAITING_TO_MERGE = "waiting to merge"


def task_numbers(tasks: Sequence[TaskState]) -> list[str]:
    """Outline numbers: "1", "2", … for tasks, and "2.1", "2.2", … for the fixes under task 2.

    Every fix in a chain (a fix of a fix, too) is numbered under the task that started it. A
    fix whose task isn't on the list is numbered as a task.
    """
    numbers: list[str] = []
    top_number: dict[str, str] = {}  # top-level task id -> its number
    top_of: dict[str, str] = {}  # task id -> id of the top-level task it belongs to
    fixes_so_far: Counter[str] = Counter()
    for state in tasks:
        # A fix comes after the task it fixes, so that one is already placed if present.
        if state.fixes is None or state.fixes not in top_of:
            top = state.id
            top_number[top] = str(len(top_number) + 1)
            numbers.append(top_number[top])
        else:
            top = top_of[state.fixes]
            fixes_so_far[top] += 1
            numbers.append(f"{top_number[top]}.{fixes_so_far[top]}")
        top_of[state.id] = top
    return numbers


@dataclass(frozen=True)
class RowNotes:
    """What a task's row says that its task doesn't: that it's ``waiting`` for a coder, as
    this note says, the ``lane`` its role runs in, if known, whether what its role's agents
    spend is unknown (``usage_unknown``), as their runtime doesn't report it, that its work
    waits for the holds on merges to lift (``merging``), as this note says, the skills its role
    gives its agents (``role_skills``), and those the project's ``[[skills]]`` entries map
    (``mapped_skills``)."""

    waiting: str | None = None
    lane: str | None = None
    usage_unknown: bool = False
    merging: str | None = None
    role_skills: tuple[str, ...] = ()
    mapped_skills: tuple[str, ...] = ()


NO_NOTES = RowNotes()
EMPTY_USAGE = Usage()  # Built once: rows compare their usage with it on every redraw.


def task_label(
    state: TaskState,
    number: str,
    frame: str,
    *,
    width: int | None = None,
    notes: RowNotes = NO_NOTES,
) -> Text:
    """A row: number, status, title and its role's tag, with the settings lined up under the
    title, and under them why the task ended, if it has a reason.

    Fix tasks are indented under the task they belong to. A running task shows the
    spinner ``frame``. A pending task about to run says it's waiting for a coder, if
    ``notes`` say so, and they give its role's lane and whether its usage is unknown. The
    reason's line is cut to ``width`` cells with an ellipsis, if given.
    """
    head = f"{number}. " if state.fixes is None else f"   {number} "
    icon = frame if state.status == "running" else STATUS_ICONS[state.status]
    # Long prompts (like fix tasks) are titled by their first line.
    title = state.task.prompt.partition("\n")[0]
    label = Text(f"{head}{icon} {title}")
    label.append_text(role_tag(state.task.role, notes.lane))
    label.append("\n")
    indent = " " * (len(head) + cell_len(icon) + 1)
    label.append(indent + _details(state, notes), style="dim")
    if state.reason:
        label.append("\n")
        label.append_text(_reason_line(f"{indent}↳ {state.reason}", width))
    return label


def role_tag(role: str, lane: str | None) -> Text:
    """The dim tag after a row's title naming its ``role`` and the ``lane`` that role runs in;
    just the role for the work lane, or a lane not known."""
    lane_part = "" if lane is None or lane == WORK_LANE else f" · {lane}"
    return Text(f"  {role}{lane_part}", style="dim")


def role_lanes(agents: Iterable[AgentEntry]) -> dict[str, str]:
    """Each role among the catalog's ``agents``, with the lane it runs in, in their order."""
    return {agent.name: agent.lane for agent in agents}


def role_skills(agents: Iterable[AgentEntry]) -> dict[str, tuple[str, ...]]:
    """Each role among the catalog's ``agents``, with the skills it gives its agents."""
    return {agent.name: agent.skills for agent in agents}


def usage_unknown_roles(agents: Iterable[AgentEntry]) -> frozenset[str]:
    """The roles among the catalog's ``agents`` whose agents' usage is unknown."""
    return frozenset(agent.name for agent in agents if not agent.usage_known)


def lanes_present(tasks: Iterable[TaskState], lanes: Mapping[str, str]) -> tuple[str, ...]:
    """The lanes some of ``tasks`` run in, as ``lanes`` gives each role's, in its order; a role
    it doesn't list has no lane."""
    used = {lanes.get(state.task.role) for state in tasks}
    return tuple(dict.fromkeys(lane for lane in lanes.values() if lane in used))


def in_lane(
    tasks: Sequence[TaskState], lanes: Mapping[str, str], lane: str | None
) -> tuple[TaskState, ...]:
    """Those of ``tasks`` whose role runs in ``lane``, as ``lanes`` gives each role's; all of
    them for none."""
    if lane is None:
        return tuple(tasks)
    return tuple(state for state in tasks if lanes.get(state.task.role) == lane)


def next_lane(lane: str | None, present: Sequence[str]) -> str | None:
    """The lane filter after ``lane``: the first of the lanes ``present`` after all (none),
    each in turn, then all again after the last; all, too, after a lane no longer present."""
    cycle = (None, *present)
    if lane not in cycle:
        return None
    return cycle[(cycle.index(lane) + 1) % len(cycle)]


def _details(state: TaskState, notes: RowNotes) -> str:
    """What a row says under its title: the settings, the fix it's on, what it spent, or that
    that's unknown if ``notes`` say so, that it's waiting for a coder, if pending and they say
    so, that a role edited it, if it's a draft it did, the skills its agent gets, if any, and
    that its work waits to merge, if they say so."""
    details = " · ".join(settings_fields(state.task.settings))
    if state.attempt:
        details += f" · fix {state.attempt}/{state.max_attempts}"
    details += _spent(state.usage, notes.usage_unknown)
    if notes.waiting is not None and state.status == "pending":
        details += f" · {notes.waiting}"
    if state.edited and state.status == "draft":
        details += f" · edited by {state.proposed_by}"
    skills = _skills(notes.role_skills, state.task.skills, notes.mapped_skills)
    return details + skills + _merging(notes.merging)


def _merging(note: str | None) -> str:
    """What a row's details end with while its task's work waits to merge, as ``note`` says:
    ``· waiting to merge``; nothing while it doesn't."""
    return "" if note is None else f" · {note}"


def _skills(role: Sequence[str], task: Sequence[str], mapped: Sequence[str]) -> str:
    """What a row's details end with for the skills its agent gets: its ``role``'s, then, after
    a ``+``, its ``task``'s and the ``mapped`` ones, each once (``· skills: code-style + python,
    layers``); just one side when the other has none, and nothing when neither has any."""
    given = given_skills(role, task, mapped)
    sides = (given[: len(role)], given[len(role) :])
    shown = " + ".join(skills_text(side) for side in sides if side)
    return f" · skills: {shown}" if shown else ""


def _spent(usage: Usage, unknown: bool) -> str:
    """What a row's details end with for what its task spent, ``usage``: that it's unknown, if
    it is, else the usage, if any."""
    if unknown:
        return f" · {USAGE_UNKNOWN}"
    return "" if usage == EMPTY_USAGE else f" · {usage_label(usage)}"


def _reason_line(line: str, width: int | None) -> Text:
    """``line`` in dim red, cut to ``width`` cells with an ellipsis if given."""
    text = Text(line, style=f"dim {ERROR}")
    if width is not None:
        text.truncate(width, overflow="ellipsis")
    return text


def session_usage_subtitle(usage: Usage) -> str:
    """The task list's border subtitle: what the session has spent, ``usage``; empty when it
    has spent nothing."""
    return "" if usage == EMPTY_USAGE else f"session: {usage_label(usage)}"


def acceptable(state: TaskState) -> bool:
    """Whether ``state`` can be accepted: only a draft can."""
    return state.status == "draft"


def ticking(tasks: Sequence[TaskState]) -> bool:
    """Whether the spinners turn: while any of ``tasks`` runs."""
    return any(state.status == "running" for state in tasks)


def new_task_row() -> Text:
    """The task list's trailing row, to add a task."""
    return Text(NEW_TASK_LABEL, style="dim italic")


def active_task(tasks: Sequence[TaskState]) -> int | None:
    """The index of the topmost task that's running or verifying, in any role; None if none."""
    return next((i for i, t in enumerate(tasks) if t.status in ACTIVE_STATUSES), None)


def edit_refusal(state: TaskState) -> str | None:
    """Why ``state`` can't be edited, as the backend would refuse it; None if it can."""
    refusal = shared_edit_refusal(state)
    return None if refusal is None else refusal.message


def task_list_title(
    *, held: bool, lane: str | None = None, merge_holds: Sequence[str] = (), findings: int = 0
) -> str:
    """The task list's border title, which says which sets, ``merge_holds``, and how many
    blocking ``findings`` hold merges, if any, when the list is held from following, and the
    ``lane`` it's narrowed to, if any."""
    parts = [
        "Task list",
        *([merges_held(merge_holds, findings)] if merge_holds or findings else []),
        *(["held"] if held else []),
        *([lane] if lane is not None else []),
    ]
    return " · ".join(parts)


def merges_held(sets: Sequence[str], findings: int = 0) -> str:
    """What the task list's title says while the ``sets`` and ``findings`` blocking findings
    hold merges: ``merges held: stress failed``, several sets joined with commas, then
    ``blocking finding``, or ``blocking findings`` for several."""
    blocking = "blocking finding" if findings == 1 else "blocking findings"
    parts = [*([f"{', '.join(sets)} failed"] if sets else []), *([blocking] if findings else [])]
    return f"merges held: {', '.join(parts)}"


def follow_top(
    rows: Sequence[tuple[int, int]], index: int, view_top: int, view_height: int
) -> int | None:
    """The scroll position that brings row ``index`` just into view, with the row above it
    too where there's room; None when the row is already in view.

    ``rows`` are each row's first line and height, and the view shows ``view_height`` lines
    from line ``view_top``.
    """
    top, height = rows[index]
    bottom = top + height
    if view_top <= top and bottom <= view_top + view_height:
        return None
    if top >= view_top:  # Below the view: bring its bottom up to the view's bottom.
        return min(bottom - view_height, top)
    context = rows[index - 1][0] if index else top
    return context if bottom - context <= view_height else top


_LabelKey = tuple[TaskState, str, int, RowNotes]


def _same(last: _LabelKey, key: _LabelKey) -> bool:
    """Whether a row labelled from ``last`` reads as one labelled from ``key`` would; the
    task is compared by identity first, as unchanged tasks usually are the same object."""
    return (last[0] is key[0] or last[0] == key[0]) and last[1:] == key[1:]


class TaskBoardView(Protocol):
    """What the task list shows and what the user has done in it: the widget, which keeps the
    highlight and the scroll position as the user changes them, and is measured as it's laid
    out."""

    @property
    def highlighted(self) -> int | None:
        """The row highlighted, if any; the new-task row comes after the tasks'."""
        ...

    @property
    def row_width(self) -> int:
        """The cells a row's line has."""
        ...

    @property
    def scroll_top(self) -> int:
        """The first line in view."""
        ...

    @property
    def view_height(self) -> int:
        """How many lines are in view."""
        ...

    def row_lines(self) -> Sequence[tuple[int, int]]:
        """Each row's first line and height, once the list is measured; none before."""
        ...

    def show_title(self, title: str) -> None:
        """Show ``title`` over the list."""
        ...

    def show_usage(self, usage: str) -> None:
        """Show ``usage`` under the list, empty for none."""
        ...

    def show_labels(self, rows: Sequence[tuple[str, Text]], trailing: Text) -> None:
        """Show ``rows``, each a task's id and its label, then the row ``trailing``."""
        ...

    def highlight(self, index: int) -> None:
        """Highlight row ``index``."""
        ...

    def scroll_to_line(self, top: int) -> None:
        """Scroll at once so line ``top`` is the first in view."""
        ...

    def keep_ticking(self, running: bool) -> None:
        """Have :meth:`TaskBoard.redraw` called as fast as spinners turn while ``running``,
        and not otherwise."""
        ...

    def edit_task(self, task: TaskState | None) -> None:
        """Ask for ``task`` to be edited, or a new task for none."""
        ...

    def accept_task(self, task_id: str) -> None:
        """Ask for draft task ``task_id`` to be accepted."""
        ...

    def remove_task(self, task_id: str) -> None:
        """Ask for task ``task_id`` to be removed."""
        ...

    def show_why(self, state: TaskState, number: str) -> None:
        """Show why ``state``, task ``number`` on the list, ended."""
        ...


class TaskBoard:
    """The backend's task list as ``view`` shows it, a trailing row to add a task after its
    tasks, telling the time for its spinners with ``clock``.

    It numbers the tasks and labels their rows at the view's width, the task about to run
    saying when it waits for a coder and each task its role's lane, and whether its role's
    usage is unknown, and redraws as the tasks change and while any runs. While sets hold merges,
    its title names them, and the tasks whose work waits for them say so. Narrowed to a lane, it
    shows just that lane's tasks, numbered as on the whole list, until widened. It totals what
    the session's turns cost. While following, it scrolls to keep the active task
    (``active_task``) in view; held, it stays where it is, until let go.
    """

    def __init__(self, view: TaskBoardView, clock: Callable[[], float]) -> None:
        self._view = view
        self._clock = clock
        self.tasks: tuple[TaskState, ...] = ()
        self.shown: tuple[TaskState, ...] = ()  # The tasks in the lane shown, all for none.
        self.numbers: list[str] = []
        self.numbers_by_id: dict[str, str] = {}
        self.session_usage = Usage()
        self.waiting: tuple[str, str] | None = None  # The task waiting for a coder, and its note.
        self.lanes: dict[str, str] = {}  # Each role's lane, as the catalog lists them.
        self.usage_unknown: frozenset[str] = frozenset()  # Roles whose usage is unknown.
        self.role_skills: dict[str, tuple[str, ...]] = {}  # Each role's skills, as listed.
        self.mapped_skills: tuple[str, ...] = ()  # Those the project's [[skills]] map.
        self.lane: str | None = None  # The lane the list is narrowed to, if any.
        self.merge_holds: tuple[MergeHold, ...] = ()  # The sets holding merges, if any.
        self.waiting_to_merge: frozenset[str] = frozenset()  # Tasks whose work waits for them.
        self.following = True
        self._labels: dict[str, tuple[_LabelKey, Text]] = {}  # Each shown row's last label.

    def start(self) -> None:
        """Show the list's title and its rows, following."""
        self._show_title()
        self.redraw()

    def show(self, tasks: Sequence[TaskState]) -> None:
        """Show ``tasks``, those in the lane shown, turning their spinners while any runs and
        following the active one; nothing for the tasks already shown."""
        tasks = tuple(tasks)
        if tasks == self.tasks:
            return
        self.tasks, self.numbers = tasks, task_numbers(tasks)
        self.numbers_by_id = dict(zip((t.id for t in tasks), self.numbers, strict=True))
        self._narrow()
        self._view.keep_ticking(ticking(tasks))

    def show_lanes(self, lanes: Mapping[str, str]) -> None:
        """Tag each task with its role's lane as ``lanes`` gives it, and narrow the list by
        them; nothing for the lanes already known."""
        lanes = dict(lanes)
        if lanes == self.lanes:
            return
        self.lanes = lanes
        self._narrow()

    def show_usage_unknown(self, roles: frozenset[str]) -> None:
        """Have the tasks in ``roles`` say what they spent is unknown, and those in other roles
        what they spent; nothing for the roles already known."""
        if roles == self.usage_unknown:
            return
        self.usage_unknown = roles
        self.redraw()

    def show_role_skills(self, skills: Mapping[str, tuple[str, ...]]) -> None:
        """Have each task's row show the skills its role gives, as ``skills`` gives each role's;
        nothing for the skills already known."""
        skills = dict(skills)
        if skills == self.role_skills:
            return
        self.role_skills = skills
        self.redraw()

    def show_mapped_skills(self, skills: Sequence[str]) -> None:
        """Have each task's row show ``skills``, those the project's ``[[skills]]`` entries map,
        after its own; nothing for the skills already known."""
        skills = tuple(skills)
        if skills == self.mapped_skills:
            return
        self.mapped_skills = skills
        self.redraw()

    def cycle_lane(self) -> None:
        """Narrow the list to the next of the lanes its tasks run in, all of them once more
        after the last, saying so in its title."""
        self.lane = next_lane(self.lane, lanes_present(self.tasks, self.lanes))
        self._show_title()
        self._narrow()

    def widen(self) -> None:
        """Show all the tasks again, the list no longer narrowed to a lane, saying so in its
        title; nothing if not narrowed."""
        if self.lane is None:
            return
        self.lane = None
        self._show_title()
        self._narrow()

    def _narrow(self) -> None:
        """Show the tasks in the lane shown, following the active one."""
        self.shown = in_lane(self.tasks, self.lanes, self.lane)
        self.redraw()
        self.follow()

    @property
    def held_sets(self) -> tuple[str, ...]:
        """The names of the sets holding merges, in the order their holds started."""
        return tuple(hold.set for hold in self.merge_holds if hold.finding is None)

    @property
    def held_findings(self) -> tuple[str, ...]:
        """The lenses that raised the blocking findings holding merges, in the order their holds
        started."""
        return tuple(hold.set for hold in self.merge_holds if hold.finding is not None)

    def show_merge_holds(self, holds: Sequence[MergeHold], waiting: Iterable[str]) -> None:
        """Title the list with the sets and findings whose ``holds`` hold merges, and have the
        tasks whose
        work is ``waiting`` for them say so; nothing for the holds and waiting work shown."""
        holds, waiting = tuple(holds), frozenset(waiting)
        if (holds, waiting) == (self.merge_holds, self.waiting_to_merge):
            return
        self.merge_holds, self.waiting_to_merge = holds, waiting
        self._show_title()
        self.redraw()

    def _show_title(self) -> None:
        """Title the list with the sets and blocking findings holding merges, whether it's held
        and the lane it's narrowed to."""
        title = task_list_title(
            held=not self.following,
            lane=self.lane,
            merge_holds=self.held_sets,
            findings=len(self.held_findings),
        )
        self._view.show_title(title)

    def show_waiting(self, waiting: tuple[str, str] | None) -> None:
        """Have the task ``waiting`` names say it waits for a coder as it says, or none do."""
        if waiting == self.waiting:
            return
        self.waiting = waiting
        self.redraw()

    def redraw(self) -> None:
        """Label the shown tasks' rows afresh, as the spinners are now and at the view's
        width."""
        frame, width = spinner_frame(self._clock()), self._view.row_width
        labels = {t.id: self._label(t, frame, width) for t in self.shown}
        self._labels = labels
        rows = [(task_id, label) for task_id, (_, label) in labels.items()]
        self._view.show_labels(rows, new_task_row())

    def _label(self, state: TaskState, frame: str, width: int) -> tuple[_LabelKey, Text]:
        """``state``'s row label and what it was made from: the last one, unless the task, its
        number, notes or the ``width`` changed, or it runs, so its spinner shows ``frame``."""
        key = (state, self.numbers_by_id[state.id], width, self._notes(state))
        last = self._labels.get(state.id)
        if last is not None and state.status != "running" and _same(last[0], key):
            return last
        number, _, notes = key[1:]
        return key, task_label(state, number, frame, width=width, notes=notes)

    def _notes(self, state: TaskState) -> RowNotes:
        """What ``state``'s row says beyond its task: the wait for a coder, if it's the task
        waiting, its role's lane, whether its role's usage is unknown, the wait to merge, if
        its work waits for the holds on merges, and its role's and the mapped skills."""
        waiting_id, note = self.waiting or (None, None)
        role = state.task.role
        waiting = note if state.id == waiting_id else None
        merging = WAITING_TO_MERGE if state.id in self.waiting_to_merge else None
        return RowNotes(
            waiting,
            self.lanes.get(role),
            role in self.usage_unknown,
            merging,
            self.role_skills.get(role, ()),
            self.mapped_skills,
        )

    def follow(self) -> None:
        """Scroll just enough to bring the active task shown into view, if following and the
        list is measured; the highlight stays where it is."""
        index = active_task(self.shown)
        if not self.following or index is None:
            return
        rows = self._view.row_lines()
        if index >= len(rows):  # Not measured yet, before the list has a size.
            return
        top = follow_top(rows, index, self._view.scroll_top, self._view.view_height)
        if top is not None:
            self._view.scroll_to_line(top)

    def hold(self) -> None:
        """Stop following, so the list stays where the user put it."""
        self.following = False
        self._show_title()

    def let_go(self) -> None:
        """Follow the active task again, scrolling to it at once; nothing if not held."""
        if self.following:
            return
        self.following = True
        self._show_title()
        self.follow()

    def activated(self, focused: bool) -> None:
        """Highlight the active task shown, if there is one, now the list is activated, if it
        took the focus (``focused``), as a disabled list doesn't."""
        index = active_task(self.shown)
        if focused and index is not None:
            self._view.highlight(index)

    @property
    def highlighted_task(self) -> TaskState | None:
        """The task the highlight is on; none on the new-task row or with nothing highlighted."""
        index = self._view.highlighted
        return self.shown[index] if index is not None and index < len(self.shown) else None

    def select(self) -> None:
        """Ask to edit the task highlighted, or a new task on the new-task row; nothing with
        nothing highlighted."""
        if self._view.highlighted is not None:
            self._view.edit_task(self.highlighted_task)

    def accept(self) -> None:
        """Ask to accept the task highlighted, if it's a draft."""
        state = self.highlighted_task
        if state is not None and acceptable(state):
            self._view.accept_task(state.id)

    def remove(self) -> None:
        """Ask to remove the task highlighted, if any."""
        state = self.highlighted_task
        if state is not None:
            self._view.remove_task(state.id)

    def explain(self) -> None:
        """Show why the task highlighted ended, if it has a reason."""
        state = self.highlighted_task
        if state is not None and state.reason:
            self._view.show_why(state, self.numbers_by_id[state.id])

    def count(self, usage: Usage) -> None:
        """Add what a turn cost, ``usage``, to the session's total."""
        self.reset_usage(self.session_usage + usage)

    def reset_usage(self, usage: Usage) -> None:
        """Start the session's total afresh at ``usage``; a session that has spent nothing
        shows no total."""
        self.session_usage = usage
        self._view.show_usage(session_usage_subtitle(usage))

    def find(self, task_id: str) -> TaskState | None:
        """The task with id ``task_id``, if it's on the list."""
        return next((t for t in self.tasks if t.id == task_id), None)
