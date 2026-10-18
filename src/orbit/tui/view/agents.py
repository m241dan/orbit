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
    RunningAgent,
    Snapshot,
)

NO_AGENTS_LABEL = "No agents running"


@dataclass(frozen=True)
class AgentRow:
    """A running agent: which, in what role, on which task, and its unanswered orbit calls
    as ``(request_id, tool)`` pairs, oldest first."""

    agent_id: str
    role: str
    task_id: str
    open_requests: tuple[tuple[str, str], ...] = ()


def track_agents(rows: tuple[AgentRow, ...], event: Event) -> tuple[AgentRow, ...]:
    """The running agents after ``event``; a snapshot replaces them."""
    if isinstance(event, Snapshot):
        return tuple(_row(agent) for agent in event.agents)
    if isinstance(event, AgentRequest | AgentReply):
        return _track_requests(rows, event)
    return _track_starts(rows, event)


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
    """``agent`` as a row, with its open requests."""
    requests = tuple((r.request_id, r.tool) for r in agent.open_requests)
    return AgentRow(agent.agent_id, agent.role, agent.task_id, requests)


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
