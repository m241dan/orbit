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
    """A running agent: which, in what role, on which task, and the orbit tool it waits on."""

    agent_id: str
    role: str
    task_id: str
    waiting_on: str | None = None


def track_agents(rows: tuple[AgentRow, ...], event: Event) -> tuple[AgentRow, ...]:
    """The running agents after ``event``."""
    if isinstance(event, AgentStarted):
        return (*rows, AgentRow(event.agent_id, event.role, event.task_id))
    if isinstance(event, AgentExited):
        return tuple(row for row in rows if row.agent_id != event.agent_id)
    if isinstance(event, AgentRequest | AgentReply):
        return _waiting(rows, event.agent_id, getattr(event, "tool", None))
    return rows


def _waiting(rows: tuple[AgentRow, ...], agent: str, tool: str | None) -> tuple[AgentRow, ...]:
    return tuple(replace(r, waiting_on=tool) if r.agent_id == agent else r for r in rows)


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
    doing = f"waiting on {row.waiting_on}" if row.waiting_on else "working"
    return f"{row.agent_id} {row.role} · {task} · {doing}"


def review_line(status: ReviewStatus | None) -> str:
    """Whether review is paused, for the agents pane; empty when there's no review."""
    if status is None:
        return ""
    if status.paused:
        return "review: paused"
    return "review: active" + (f" · {status.open_batches} open" if status.open_batches else "")
