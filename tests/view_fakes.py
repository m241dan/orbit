"""Fake views for the TUI's models: what each shows, as a widget or screen would show it, and
what the user does in it, as a test says."""

from collections.abc import Sequence
from math import ceil

from rich.text import Text

from orbit.protocol import ChecksProposed, DecideProposal, ReportShown, ReportSummary, TaskState
from orbit.tui.view.common import kept_highlight
from orbit.tui.view.proposals import Decided
from orbit.tui.view.settings import Chosen
from orbit.tui.view.takeover import Answered


class TaskView:
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


class AgentsView:
    """What the agents list shows, as it shows it: the lines of each listing, in turn, the keys
    of the last and the review line; and the row highlighted, which a test sets as the user
    moves. ``cancels`` has the agents asked to be cancelled."""

    def __init__(self) -> None:
        self.highlighted: int | None = None
        self.listings: list[list[str]] = []
        self.keys: list[str] = []
        self.review = ""
        self.cancels: list[str] = []

    def list_agents(self, rows: Sequence[tuple[str, str]]) -> None:
        self.keys = [key for key, _ in rows]
        self.listings.append([line for _, line in rows])

    def show_review_line(self, line: str) -> None:
        self.review = line

    def ask_to_cancel(self, agent_id: str) -> None:
        self.cancels.append(agent_id)


class StepsView:
    """What the steps show, as they show them: each text drawn, whether it was laid out again,
    and whether they tick. Text wraps at ``width`` characters, unknown while it's None."""

    def __init__(self, width: int | None = 40) -> None:
        self.width = width
        self.texts: list[str] = []
        self.layouts: list[bool] = []
        self.ticking = False

    def wrapped_height(self, text: str) -> int | None:
        if self.width is None:
            return None
        width = self.width
        return sum(max(1, ceil(len(line) / width)) for line in text.split("\n"))

    def show_text(self, text: str, layout: bool) -> None:
        self.texts.append(text)
        self.layouts.append(layout)

    def keep_ticking(self, running: bool) -> None:
        self.ticking = running


class HaltView:
    """What the halt shows, as it shows it: the status line, and the text each modal opened
    shows, in the order they opened; a test closes the one open as the user would, by setting
    ``modal_open``. ``closes`` counts the modals closed from the halt."""

    def __init__(self) -> None:
        self.status: str | None = None
        self.modals: list[str] = []
        self.modal_open = False
        self.closes = 0

    def show_status(self, status: str | None) -> None:
        self.status = status

    def open_modal(self, explanation: str) -> None:
        assert not self.modal_open
        self.modals.append(explanation)
        self.modal_open = True

    def show_in_modal(self, explanation: str) -> None:
        assert self.modal_open
        self.modals[-1] = explanation

    def close_modal(self) -> None:
        if self.modal_open:
            self.closes += 1
            self.modal_open = False


class OfferView:
    """What the offer shows: the modals opened, by what takes each one's answer; a test answers
    the one open as the user would, with :meth:`answer`. ``closes`` counts the modals closed
    from the offer."""

    def __init__(self) -> None:
        self.modals: list[Answered] = []
        self.modal_open = False
        self.closes = 0

    def open_modal(self, answered: Answered) -> None:
        assert not self.modal_open
        self.modals.append(answered)
        self.modal_open = True

    def close_modal(self) -> None:
        if self.modal_open:
            self.closes += 1
            self.modal_open = False

    async def answer(self, take: bool | None) -> None:
        """Answer the modal open, taking the repository over or not, or closing it with none."""
        assert self.modal_open
        self.modal_open = False
        await self.modals[-1](take)


class ProposalModal:
    """Where a :class:`ProposalQueue` shows proposals, as the app's modal slot does: what it
    opened, in turn, what it closed and the proposal shown. Only the proposal shown can be
    decided: a closed one's screen answers nothing."""

    def __init__(self) -> None:
        self.opened: list[ChecksProposed] = []
        self.closed: list[ChecksProposed] = []
        self.shown: ChecksProposed | None = None
        self._decided: Decided | None = None

    def open(self, proposal: ChecksProposed, decided: Decided) -> None:
        self.opened.append(proposal)
        self.shown, self._decided = proposal, decided

    def close(self) -> None:
        if self.shown is not None:
            self.closed.append(self.shown)
        self.shown = self._decided = None

    async def decide(self, decision: DecideProposal | None) -> None:
        """Have the user decide on the proposal shown, or close it undecided."""
        decided = self._decided
        assert decided is not None
        self.shown = self._decided = None
        await decided(decision)


class PickerScreens:
    """Where the pickers show: the labels the model picker and the role picker were given, in
    turn, and each picker opened, with the option it opened on and what takes the pick."""

    def __init__(self) -> None:
        self.labels: list[list[str | Text]] = []
        self.roles: list[list[str]] = []
        self.opened: list[tuple[str, int | None, Chosen]] = []

    def label_models(self, labels: Sequence[str | Text]) -> None:
        self.labels.append(list(labels))

    def pick_model(self, current: int | None, chosen: Chosen) -> None:
        self.opened.append(("model", current, chosen))

    def pick_tool_access(self, current: int | None, chosen: Chosen) -> None:
        self.opened.append(("tools", current, chosen))

    def pick_effort(self, current: int | None, chosen: Chosen) -> None:
        self.opened.append(("effort", current, chosen))

    def pick_role(self, labels: Sequence[str], current: int | None, chosen: Chosen) -> None:
        self.roles.append(list(labels))
        self.opened.append(("role", current, chosen))

    async def pick(self, index: int) -> None:
        """Pick option ``index`` in the picker opened last."""
        _, _, chosen = self.opened[-1]
        result = chosen(index)
        if result is not None:
            await result


class ReportsView:
    """What the shelf shows: whether the list is open, the lists shown in it, in turn, and the
    reports shown over it."""

    def __init__(self) -> None:
        self.list_open = False
        self.lists: list[tuple[ReportSummary, ...]] = []
        self.reports: list[ReportShown] = []

    def open_list(self) -> None:
        self.list_open = True

    def show_list(self, reports: Sequence[ReportSummary]) -> None:
        self.lists.append(tuple(reports))

    def show_report(self, report: ReportShown) -> None:
        self.reports.append(report)
