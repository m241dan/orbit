"""The agents pane."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol

from orbit.protocol import (
    PLAN_TASK,
    STARTUP_TASK,
    AgentExited,
    AgentReply,
    AgentRequest,
    AgentStarted,
    Event,
    ReviewStatus,
    RunningAgent,
    Snapshot,
    TurnFinished,
    Usage,
)
from orbit.tui.view.tasks import cost_text

NO_AGENTS_LABEL = "No agents running"


@dataclass(frozen=True)
class AgentRow:
    """A running agent: which, in what role, on which task, and its unanswered orbit calls
    as ``(request_id, tool)`` pairs, oldest first, and what its finished turns cost (``spent``)."""

    agent_id: str
    role: str
    task_id: str
    open_requests: tuple[tuple[str, str], ...] = ()
    spent: Usage = field(default_factory=Usage)


def track_agents(rows: tuple[AgentRow, ...], event: Event) -> tuple[AgentRow, ...]:
    """The running agents after ``event``; a snapshot replaces them."""
    if isinstance(event, Snapshot):
        return tuple(_row(agent) for agent in event.agents)
    if isinstance(event, AgentRequest | AgentReply):
        return _track_requests(rows, event)
    if isinstance(event, TurnFinished):
        return tuple(_count(row, event) for row in rows)
    return _track_starts(rows, event)


def _count(row: AgentRow, event: TurnFinished) -> AgentRow:
    """``row`` with what ``event``'s turn cost added, if it's ``row``'s agent's."""
    if row.agent_id != event.agent_id or event.usage is None:
        return row
    return replace(row, spent=row.spent + event.usage)


def _track_starts(rows: tuple[AgentRow, ...], event: Event) -> tuple[AgentRow, ...]:
    """``rows`` with the agent ``event`` starts added, unless it's there, or the one it exits
    removed."""
    if isinstance(event, AgentStarted) and not _shown(rows, event.agent_id):
        return (*rows, AgentRow(event.agent_id, event.role, event.task_id))
    if isinstance(event, AgentExited):
        return tuple(row for row in rows if row.agent_id != event.agent_id)
    return rows


def _shown(rows: tuple[AgentRow, ...], agent_id: str) -> bool:
    """Whether agent ``agent_id`` has a row in ``rows``."""
    return any(row.agent_id == agent_id for row in rows)


def _row(agent: RunningAgent) -> AgentRow:
    """``agent`` as a row, with its open requests and what it has cost so far."""
    requests = tuple((r.request_id, r.tool) for r in agent.open_requests)
    return AgentRow(agent.agent_id, agent.role, agent.task_id, requests, agent.spent)


def _track_requests(
    rows: tuple[AgentRow, ...], event: AgentRequest | AgentReply
) -> tuple[AgentRow, ...]:
    """``rows`` with the request ``event`` makes opened, or the one it answers closed."""
    return tuple(_answer(row, event) for row in rows)


def _answer(row: AgentRow, event: AgentRequest | AgentReply) -> AgentRow:
    """``row`` with ``event``'s request opened, or the request it replies to closed, if
    ``event`` is from ``row``'s agent."""
    if row.agent_id != event.agent_id:
        return row
    if isinstance(event, AgentRequest):
        return _open(row, event)
    still_open = tuple(pair for pair in row.open_requests if pair[0] != event.request_id)
    return replace(row, open_requests=still_open)


def _open(row: AgentRow, event: AgentRequest) -> AgentRow:
    """``row`` with ``event``'s request opened, unless it's open already."""
    if any(pair[0] == event.request_id for pair in row.open_requests):
        return row
    opened = (*row.open_requests, (event.request_id, event.tool))
    return replace(row, open_requests=opened)


def agent_line(row: AgentRow, number: str | None) -> str:
    """``row`` as a line, ending with what its agent has cost so far once it has; ``number`` is
    its task's number in the list, if it has one there."""
    if number is not None:
        task = f"task {number}"
    elif row.task_id == STARTUP_TASK:
        task = "base commit"
    elif row.task_id == PLAN_TASK:
        task = "planning"
    else:
        task = f"task {row.task_id}"
    doing = f"waiting on {row.open_requests[0][1]}" if row.open_requests else "working"
    cost = cost_text(row.spent)
    return " · ".join([f"{row.agent_id} {row.role}", task, doing, *([cost] if cost else [])])


def review_line(status: ReviewStatus | None) -> str:
    """Whether review is paused, for the agents pane; empty when there's no review."""
    if status is None:
        return ""
    if status.paused:
        return "review: paused"
    return "review: active" + (f" · {status.open_batches} open" if status.open_batches else "")


class AgentsView(Protocol):
    """What shows the agents running, a row each, and the review line, and what the user
    highlights there, as the widget keeps it."""

    @property
    def highlighted(self) -> int | None:
        """The row highlighted, if any."""
        ...

    def list_agents(self, rows: Sequence[tuple[str, str]]) -> None:
        """List ``rows``, each an agent's id and its line, or the empty row for none."""
        ...

    def show_review_line(self, line: str) -> None:
        """Show ``line``, saying whether review is paused."""
        ...

    def ask_to_cancel(self, agent_id: str) -> None:
        """Ask to cancel agent ``agent_id``'s work."""
        ...


class AgentsList:
    """The agents running, shown in ``view`` a row each by what each is doing, its task by its
    number in the task list, and whether review is paused; cancelling asks to cancel the
    highlighted agent's work."""

    def __init__(self, view: AgentsView) -> None:
        self._view = view
        self.rows: tuple[AgentRow, ...] = ()
        self._numbers: Mapping[str, str] = {}

    def show(self) -> None:
        """Show the rows, the empty row when no agents are running."""
        numbers = self._numbers
        self._view.list_agents(
            [(row.agent_id, agent_line(row, numbers.get(row.task_id))) for row in self.rows]
        )

    def track(self, event: Event) -> None:
        """Follow the agents through ``event``, showing them again if they changed."""
        rows = track_agents(self.rows, event)
        if rows != self.rows:
            self.rows = rows
            self.show()

    def number_tasks(self, numbers: Mapping[str, str]) -> None:
        """Task ids' numbers in the task list, to show agents' tasks by; shows the agents again
        only if one of their numbers changed."""
        changed = self._shown_numbers(numbers) != self._shown_numbers(self._numbers)
        self._numbers = numbers
        if changed:
            self.show()

    def _shown_numbers(self, numbers: Mapping[str, str]) -> list[str | None]:
        return [numbers.get(row.task_id) for row in self.rows]

    def show_review(self, status: ReviewStatus | None) -> None:
        """Show whether review is paused, as ``status`` says."""
        self._view.show_review_line(review_line(status))

    def cancel(self) -> None:
        """Ask to cancel the highlighted agent's work, if an agent is highlighted."""
        highlighted = self._view.highlighted
        if highlighted is not None and highlighted < len(self.rows):
            self._view.ask_to_cancel(self.rows[highlighted].agent_id)
