"""Conversation tabs."""

from collections import Counter
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TypeGuard, get_args

from orbit.protocol import (
    BUILT_IN_ROLE_NAMES,
    PLANNER,
    PLANNER_LANE,
    AgentExited,
    AgentReply,
    AgentRequest,
    AgentStarted,
    AssistantMessage,
    BusyLane,
    ErrorOccurred,
    Event,
    ProgressNoted,
    RunningAgent,
    TaskGaveUp,
    TaskState,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
)

ALL_TAB_LABEL = "All"
"""The fixed first tab, showing every conversation; it isn't one of the tracked tabs."""

ROLE_TABS: tuple[str, ...] = BUILT_IN_ROLE_NAMES
"""The built-in roles, in strip order, that each have a pinned tab shared by all their
agents; a pinned tab's key and label are its role's name. A conversational role's (the
planner's, ``PLANNER``, and any other the catalog lists) also keys the user's prompts to it."""


RUNNING_MARK = "●"
"""Leads a tab's label while its role is at work."""


@dataclass(frozen=True)
class ConversationTab:
    """A conversation tab, by ``key`` (a pinned role's name, or a running agent's id),
    whether it has output not yet looked at, how many of its role's agents are at work
    (``running``), whether it's ``pinned``, there whether or not its role's agents run, and,
    for a conversational role's, the ``lane`` its role answers in. Only a pinned tab counts
    its agents: a temporary tab is there only while its agent runs, and a conversational
    role's count follows its busy lane, as its agent runs from the first prompt until the
    session ends."""

    key: str
    role: str
    unread: bool = False
    running: int = 0
    pinned: bool = False
    lane: BusyLane | None = None


_TabOutput = (
    AssistantMessage
    | ToolCallStarted
    | ToolCallFinished
    | AgentRequest
    | AgentReply
    | ProgressNoted
    | TaskGaveUp
    | ErrorOccurred
    | TurnFinished
)
"""Events that add an entry to their agent's conversation; a turn's end adds its rule."""

_TAB_OUTPUT: tuple[type[_TabOutput], ...] = get_args(_TabOutput)
"""The classes in ``_TabOutput``, for ``isinstance``."""


PINNED_TABS = tuple(
    ConversationTab(role, role, pinned=True, lane=PLANNER_LANE if role == PLANNER else None)
    for role in ROLE_TABS
)
"""The built-in roles' tabs, there from the start and never removed; the planner's is a
conversational role's, answering in its own lane until a catalog says otherwise."""


def pinned_roles(tabs: Iterable[ConversationTab]) -> frozenset[str]:
    """The roles with a pinned tab among ``tabs``."""
    return frozenset(tab.key for tab in tabs if tab.pinned)


def pin_conversations(
    tabs: tuple[ConversationTab, ...], lanes: Mapping[str, BusyLane]
) -> tuple[ConversationTab, ...]:
    """``tabs`` with a pinned tab for each conversational role in ``lanes``, by role to the lane
    it answers in: the built-in roles' tabs first, each following its role's lane if it's among
    them, and the planner's its own lane if it isn't; then the other roles', in their order,
    those already pinned kept as they are and those no longer listed gone; then the agents' own
    tabs."""
    lanes = {PLANNER: PLANNER_LANE, **lanes}
    built_in = (replace(tab, lane=lanes.get(tab.key)) for tab in tabs if tab.key in ROLE_TABS)
    return (*built_in, *_pinned_others(tabs, lanes), *_unpinned(tabs))


def _pinned_others(
    tabs: Iterable[ConversationTab], lanes: Mapping[str, BusyLane]
) -> Iterator[ConversationTab]:
    """The pinned tabs of the conversational roles in ``lanes`` outside the built-in ones, in
    their order, each following its role's lane: those among ``tabs`` as they are, and a new one
    for each of the rest."""
    kept = {tab.key: tab for tab in tabs if tab.pinned}
    return (
        replace(kept.get(role, ConversationTab(role, role, pinned=True)), lane=lane)
        for role, lane in lanes.items()
        if role not in ROLE_TABS
    )


def reset_tabs(tabs: tuple[ConversationTab, ...]) -> tuple[ConversationTab, ...]:
    """``tabs``' pinned tabs alone, as before any agent ran: none unread, and none at work but
    the conversational roles', which follow their busy lanes."""
    return tuple(
        replace(tab, unread=False, running=tab.running if tab.lane is not None else 0)
        for tab in tabs
        if tab.pinned
    )


def running_roles(
    agents: Iterable[RunningAgent], pinned: Collection[str] = ROLE_TABS
) -> Mapping[str, str]:
    """Which of the ``pinned`` roles each of the running ``agents`` is in, by agent id, for
    those in one."""
    return {agent.agent_id: agent.role for agent in agents if agent.role in pinned}


def follow_running(
    tabs: tuple[ConversationTab, ...], agents: Sequence[RunningAgent]
) -> tuple[ConversationTab, ...]:
    """``tabs`` agreeing with the ``agents`` running: each pinned tab counting its role's
    agents (a conversational role's following its busy lane), then an own tab for each agent
    outside the pinned roles, keeping those already open as they are."""
    return (*_count_pinned(tabs, agents), *_own_tabs(tabs, agents))


def _count_pinned(
    tabs: tuple[ConversationTab, ...], agents: Sequence[RunningAgent]
) -> tuple[ConversationTab, ...]:
    """``tabs``' pinned tabs, each counting its role's running ``agents`` but the
    conversational roles'."""
    counts = Counter(agent.role for agent in agents)
    pinned = (tab for tab in tabs if tab.pinned)
    return tuple(
        tab if tab.lane is not None else replace(tab, running=counts[tab.key]) for tab in pinned
    )


def _own_tabs(
    tabs: tuple[ConversationTab, ...], agents: Sequence[RunningAgent]
) -> tuple[ConversationTab, ...]:
    """The own tabs of the running ``agents`` outside the pinned roles: those open in ``tabs``,
    then one for each of the rest."""
    own = _own_roles(agents, pinned_roles(tabs))
    kept = tuple(tab for tab in _unpinned(tabs) if tab.key in own)
    open_keys = {tab.key for tab in kept}
    added = (ConversationTab(key, role) for key, role in own.items() if key not in open_keys)
    return (*kept, *added)


def _unpinned(tabs: Iterable[ConversationTab]) -> Iterable[ConversationTab]:
    """``tabs`` but the pinned ones."""
    return (tab for tab in tabs if not tab.pinned)


def _own_roles(agents: Iterable[RunningAgent], pinned: Collection[str]) -> dict[str, str]:
    """The role of each of the running ``agents`` outside the ``pinned`` roles, by agent id."""
    return {agent.agent_id: agent.role for agent in agents if agent.role not in pinned}


def stopped_callers(
    events: Iterable[Event], agents: Iterable[RunningAgent]
) -> tuple[str | None, ...]:
    """The agents that start tool calls or requests to orbit in ``events`` but aren't among
    the ``agents`` running, so none of their calls can still run; the planner's agent is among
    those running from its first turn until the session ends."""
    running = {agent.agent_id for agent in agents}
    callers = (e.agent_id for e in events if isinstance(e, ToolCallStarted | AgentRequest))
    return tuple(dict.fromkeys(agent for agent in callers if agent not in running))


def track_roles(
    roles: Mapping[str, str], event: Event, pinned: Collection[str] = ROLE_TABS
) -> Mapping[str, str]:
    """Which of the ``pinned`` roles each agent started in, by agent id, after ``event``."""
    if isinstance(event, AgentStarted) and event.role in pinned:
        return {**roles, event.agent_id: event.role}
    return roles


def tab_key(agent: str | None, roles: Mapping[str, str]) -> str | None:
    """Which tab ``agent``'s entries belong to: its role's, when ``roles`` has it pinned."""
    return None if agent is None else roles.get(agent, agent)


def task_tab_key(state: TaskState | None) -> str | None:
    """Which pinned tab the notes on a task (its header, verification results and merge
    notes) belong to: its role's, when the role has one. None, for All only, when the role
    has no pinned tab or the task isn't on the list (``state`` None)."""
    if state is None or state.task.role not in ROLE_TABS:
        return None
    return state.task.role


def track_tabs(
    tabs: tuple[ConversationTab, ...],
    event: Event,
    active: str | None = None,
    roles: Mapping[str, str] = MappingProxyType({}),
) -> tuple[ConversationTab, ...]:
    """The open conversation tabs after ``event``; output for a tab other than ``active``,
    the one being shown, marks it unread. Agents in a pinned role share its tab, and
    ``roles`` says which role those agents are in."""
    if _is_tab_output(event):
        return mark_tab_unread(tabs, tab_key(event.agent_id, roles), active)
    return replay_tabs(tabs, event, roles)


def replay_tabs(
    tabs: tuple[ConversationTab, ...],
    event: Event,
    roles: Mapping[str, str] = MappingProxyType({}),
) -> tuple[ConversationTab, ...]:
    """The open conversation tabs after ``event``, replayed from the session's log: agents'
    tabs open and close as with ``track_tabs``, but no output marks one unread, as it isn't
    new."""
    if isinstance(event, AgentStarted):
        return _open_tab(tabs, event)
    if isinstance(event, AgentExited):
        return _close_tab(tabs, event.agent_id, roles)
    return tabs


def _close_tab(
    tabs: tuple[ConversationTab, ...], agent: str, roles: Mapping[str, str]
) -> tuple[ConversationTab, ...]:
    """``tabs`` without ``agent``'s own tab; the pinned tabs stay, with one agent fewer at
    work on the tab of ``agent``'s role when ``roles`` has it pinned."""
    tabs = tuple(tab for tab in tabs if tab.pinned or tab.key != agent)
    return _count_running(tabs, roles.get(agent), -1)


def _is_tab_output(event: Event) -> TypeGuard[_TabOutput]:
    """Whether ``event`` adds an entry to its agent's conversation."""
    return isinstance(event, _TAB_OUTPUT)


def _open_tab(
    tabs: tuple[ConversationTab, ...], event: AgentStarted
) -> tuple[ConversationTab, ...]:
    """``tabs`` with one for the started agent, unless its role has a pinned tab, which
    counts one more agent at work instead."""
    if event.role in pinned_roles(tabs):
        return _count_running(tabs, event.role, 1)
    return (*tabs, ConversationTab(event.agent_id, event.role))


def _count_running(
    tabs: tuple[ConversationTab, ...], role: str | None, change: int
) -> tuple[ConversationTab, ...]:
    """``tabs`` with ``change`` more agents at work on the pinned tab of ``role``, which is
    None for a role with no pinned tab. A conversational role's tab is left to
    ``mark_lane_busy``."""
    return tuple(
        replace(tab, running=tab.running + change) if _counts(tab, role) else tab for tab in tabs
    )


def _counts(tab: ConversationTab, role: str | None) -> bool:
    """Whether ``tab`` counts the agents at work in ``role``: it's the role's pinned tab, and
    not a conversational role's."""
    return tab.pinned and tab.key == role and tab.lane is None


def mark_lane_busy(
    tabs: tuple[ConversationTab, ...], lane: BusyLane, busy: bool
) -> tuple[ConversationTab, ...]:
    """``tabs`` with the tabs of the conversational roles answering in ``lane`` at work while
    it's ``busy``."""
    return tuple(replace(tab, running=int(busy)) if tab.lane == lane else tab for tab in tabs)


def mark_tab_unread(
    tabs: tuple[ConversationTab, ...], key: str | None, active: str | None
) -> tuple[ConversationTab, ...]:
    """``tabs`` with the tab keyed ``key`` unread, unless it's ``active``, the one being
    shown; nothing changes for None, an entry under All only."""
    return tabs if key == active else _set_unread(tabs, key, True)


def mark_tab_read(tabs: tuple[ConversationTab, ...], agent: str) -> tuple[ConversationTab, ...]:
    """``tabs`` with the tab keyed ``agent`` no longer unread."""
    return _set_unread(tabs, agent, False)


def _set_unread(
    tabs: tuple[ConversationTab, ...], agent: str | None, unread: bool
) -> tuple[ConversationTab, ...]:
    return tuple(replace(tab, unread=unread) if tab.key == agent else tab for tab in tabs)


def tab_labels(tabs: Sequence[ConversationTab]) -> list[str]:
    """Tabs named by role: the first of a role is just the role, later ones "coder 2", …;
    tabs with agents at work start with "● ", and unread tabs end in " *"."""
    seen: Counter[str] = Counter()
    labels: list[str] = []
    for tab in tabs:
        seen[tab.role] += 1
        count = seen[tab.role]
        label = tab.role if count == 1 else f"{tab.role} {count}"
        labels.append(_marked(label, tab))
    return labels


def _marked(label: str, tab: ConversationTab) -> str:
    """``label`` with ``tab``'s running and unread markers."""
    if tab.running:
        label = f"{RUNNING_MARK} {label}"
    return f"{label} *" if tab.unread else label
