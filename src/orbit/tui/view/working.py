"""The working row below the conversation: who runs right now, and what they're doing."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from rich.cells import cell_len

from orbit.protocol import (
    PLANNER,
    PLANNER_LANE,
    AgentExited,
    Event,
    RunningAgent,
    Snapshot,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
)
from orbit.tui.view.common import clipped, spinner_frame
from orbit.tui.view.tabs import tab_key
from orbit.tui.view.tool_calls import tool_call_summary
from orbit.tui.view.transcript import speaker

WORKING_LINES_SHOWN = 3
"""The most running agents the row shows before summing up the rest in a ``+N more`` line."""


@dataclass(frozen=True)
class RunningCall:
    """A tool call that started at ``since`` and hasn't finished, summed up in ``summary``."""

    call_id: str
    summary: str
    since: float


@dataclass(frozen=True)
class WorkingAgent:
    """An agent (None for a turn orbit ended itself, with no agent) whose turn started at
    ``since`` and hasn't ended, with its unfinished ``calls`` in the order they started."""

    agent: str | None
    since: float
    calls: tuple[RunningCall, ...] = ()


type Working = tuple[WorkingAgent, ...]
"""The running agents, oldest turn first."""


def track_working(working: Working, event: Event, now: float) -> Working:
    """Who runs, and which of their calls, after ``event`` at ``now``; a snapshot replaces
    them with the agents it says are mid-turn, timed from ``now``."""
    if isinstance(event, Snapshot):
        return tuple(
            WorkingAgent(running.agent_id, now)
            for running in event.agents
            if _mid_turn(running, event)
        )
    return _track_turn(working, event, now)


def _mid_turn(running: RunningAgent, snapshot: Snapshot) -> bool:
    """Whether ``running`` is mid-turn in ``snapshot``: the planner's agent lives between its
    turns, so only while the planner's lane is busy; any other lives only while its run does."""
    return running.role != PLANNER or snapshot.busy.get(PLANNER_LANE, False)


def _track_turn(working: Working, event: Event, now: float) -> Working:
    """``working`` with the turn ``event`` starts or ends, or the call it starts or ends."""
    match event:
        case TurnStarted(agent_id=agent):
            return (*_without(working, agent), WorkingAgent(agent, now))
        case TurnFinished(agent_id=agent) | AgentExited(agent_id=agent):
            return _without(working, agent)
        case ToolCallStarted() | ToolCallFinished():
            return _track_call(working, event, now)
        case _:
            return working


def _track_call(working: Working, event: ToolCallStarted | ToolCallFinished, now: float) -> Working:
    """``working`` with ``event``'s call added to its agent's calls, or dropped once it
    finishes."""
    if isinstance(event, ToolCallStarted):
        call = RunningCall(event.call_id, tool_call_summary(event.name, event.arguments), now)
        return _with_calls(working, event.agent_id, lambda calls: (*calls, call))
    finished = event.call_id
    return _with_calls(
        working,
        event.agent_id,
        lambda calls: tuple(call for call in calls if call.call_id != finished),
    )


def _without(working: Working, agent: str | None) -> Working:
    return tuple(running for running in working if running.agent != agent)


def _with_calls(
    working: Working,
    agent: str | None,
    change: Callable[[tuple[RunningCall, ...]], tuple[RunningCall, ...]],
) -> Working:
    """``working`` with ``agent``'s calls changed by ``change``, if it's running."""
    return tuple(
        replace(running, calls=change(running.calls)) if running.agent == agent else running
        for running in working
    )


def working_label(agent: str | None, roles: Mapping[str, str]) -> str:
    """Who a working line names: ``planner`` for the planner (see :func:`speaker`), else the
    agent's id."""
    named = speaker(agent, roles)
    return PLANNER if named is None else named


def elapsed_clock(seconds: float) -> str:
    """``seconds`` as a running clock reads: ``m:ss``, or ``h:mm:ss`` from an hour."""
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def working_line(running: WorkingAgent, label: str, now: float, width: int) -> str:
    """``running``'s line, headed by ``label``: the spinner, its latest unfinished call (and
    how many others run), clipped so the line fits ``width``, that call's time and the turn's
    time."""
    head = f"{spinner_frame(now)}{label} · "
    turn = elapsed_clock(now - running.since)
    if not running.calls:
        return f"{head}{turn}"
    latest = running.calls[-1]
    others = len(running.calls) - 1
    more = f" (+{others} running)" if others else ""
    tail = f"{more} {elapsed_clock(now - latest.since)} · {turn}"
    room = max(width - cell_len(head) - cell_len(tail), 1)
    return f"{head}{clipped(latest.summary, room)}{tail}"


@dataclass(frozen=True)
class WorkingView:
    """What the working row is drawn from: the ``working`` agents, the ``roles`` that place
    them in tabs, and the tab ``shown`` (None for All)."""

    working: Working
    roles: Mapping[str, str]
    shown: str | None


def working_lines(view: WorkingView, now: float, width: int) -> tuple[str, ...]:
    """The working row's lines at ``now`` in ``width`` columns: one per agent running under
    the shown tab, oldest turn first, up to ``WORKING_LINES_SHOWN`` and then ``+N more``."""
    visible = [
        running
        for running in view.working
        if view.shown is None or tab_key(running.agent, view.roles) == view.shown
    ]
    lines = [
        working_line(running, working_label(running.agent, view.roles), now, width)
        for running in visible[:WORKING_LINES_SHOWN]
    ]
    hidden = len(visible) - WORKING_LINES_SHOWN
    if hidden > 0:
        lines.append(f"+{hidden} more")
    return tuple(lines)


class WorkingRowView(Protocol):
    """The working row, as its lines are shown in it."""

    @property
    def width(self) -> int:
        """How many columns the row has."""
        ...

    def show_lines(self, lines: Sequence[str]) -> None:
        """Show ``lines``, one per row, hiding the row for none."""
        ...


class WorkingLines:
    """What's running right now under the shown tab, one line per agent, shown in ``view``
    and timed by ``clock``."""

    def __init__(self, view: WorkingRowView, clock: Callable[[], float]) -> None:
        self._view = view
        self._clock = clock
        self.agents: Working = ()
        self.lines: tuple[str, ...] = ()  # The lines shown.
        self._roles: Mapping[str, str] = {}
        self._shown: str | None = None

    @property
    def running(self) -> bool:
        """Whether any agent runs."""
        return bool(self.agents)

    def track(self, event: Event, roles: Mapping[str, str]) -> None:
        """Follow who runs after ``event``, with ``roles`` placing agents in tabs."""
        self.agents = track_working(self.agents, event, self._clock())
        self._roles = roles
        self.tick()

    def show_tab(self, key: str | None) -> None:
        """Show what runs under the tab keyed ``key``; None is All."""
        self._shown = key
        self.tick()

    def tick(self) -> None:
        """Show the lines again, if they changed."""
        view = WorkingView(self.agents, self._roles, self._shown)
        lines = working_lines(view, self._clock(), self._view.width)
        if lines == self.lines:
            return
        self.lines = lines
        self._view.show_lines(lines)
