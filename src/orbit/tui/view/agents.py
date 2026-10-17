"""The agents pane."""

from dataclasses import dataclass, replace

from orbit.protocol import (
    PLAN_TASK,
    STARTUP_TASK,
    AgentExited,
    AgentReply,
    AgentRequest,
    AgentStarted,
    Event,
    ReviewStatus,
)


@dataclass(frozen=True)
class AgentRow:
    """A running agent: which, in what role, on which task, and its unanswered orbit calls
    as ``(request_id, tool)`` pairs, oldest first."""

    agent_id: str
    role: str
    task_id: str
    open_requests: tuple[tuple[str, str], ...] = ()


def track_agents(rows: tuple[AgentRow, ...], event: Event) -> tuple[AgentRow, ...]:
    """The running agents after ``event``."""
    if isinstance(event, AgentStarted):
        return (*rows, AgentRow(event.agent_id, event.role, event.task_id))
    if isinstance(event, AgentExited):
        return tuple(row for row in rows if row.agent_id != event.agent_id)
    if isinstance(event, AgentRequest | AgentReply):
        return _track_requests(rows, event)
    return rows


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
        opened = (*row.open_requests, (event.request_id, event.tool))
        return replace(row, open_requests=opened)
    still_open = tuple(pair for pair in row.open_requests if pair[0] != event.request_id)
    return replace(row, open_requests=still_open)


def agent_line(row: AgentRow, number: str | None) -> str:
    """``row`` as a line; ``number`` is its task's number in the list, if it has one there."""
    if number is not None:
        task = f"task {number}"
    elif row.task_id == STARTUP_TASK:
        task = "base commit"
    elif row.task_id == PLAN_TASK:
        task = "planning"
    else:
        task = f"task {row.task_id}"
    doing = f"waiting on {row.open_requests[0][1]}" if row.open_requests else "working"
    return f"{row.agent_id} {row.role} · {task} · {doing}"


def review_line(status: ReviewStatus | None) -> str:
    """Whether review is paused, for the agents pane; empty when there's no review."""
    if status is None:
        return ""
    if status.paused:
        return "review: paused"
    return "review: active" + (f" · {status.open_batches} open" if status.open_batches else "")
