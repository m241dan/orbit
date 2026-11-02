"""Conversation tabs."""

from collections import Counter
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TypeGuard, get_args

from orbit.protocol import (
    PINNED_ROLE_NAMES,
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
    TriggerEntry,
    TurnFinished,
)

ALL_TAB_LABEL = "All"
"""The fixed first tab, showing every conversation; it isn't one of the tracked tabs."""

ROLE_TABS: tuple[str, ...] = PINNED_ROLE_NAMES
"""The built-in roles, in strip order, that each have a pinned tab shared by all their
agents; a pinned tab's key and label are its role's name. A conversational role's (the
planner's, ``PLANNER``, and any other the catalog lists) also keys the user's prompts to it."""


TRIGGER_TAB_PREFIX = "tab:"
"""Leads the key of a trigger's tab, shared by the agents of every trigger naming it, so the key
can't be taken for an agent's id or a role's name."""


RUNNING_MARK = "●"
"""Leads a tab's label while its role is at work."""


@dataclass(frozen=True)
class ConversationTab:
    """A conversation tab, by ``key`` (a pinned role's name, a trigger's tab's, or a running
    agent's id), whether it has output not yet looked at, how many of its agents are at work
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
    """The keys of the pinned tabs among ``tabs``: the roles with one, and the triggers' tabs."""
    return frozenset(tab.key for tab in tabs if tab.pinned)


def trigger_tab_key(tab: str) -> str:
    """The key of the trigger's tab named ``tab``."""
    return f"{TRIGGER_TAB_PREFIX}{tab}"


def is_trigger_tab(key: str | None) -> bool:
    """Whether ``key`` is a trigger's tab's."""
    return key is not None and key.startswith(TRIGGER_TAB_PREFIX)


def pinned_key(role: str, tab: str | None, pinned: Collection[str]) -> str | None:
    """The key of the pinned tab an agent in ``role`` writes into, started by a trigger of the
    tab named ``tab``, if any: that tab's, when it's among the ``pinned`` keys, else its role's,
    when that is; None when it has a tab of its own."""
    shared = None if tab is None else trigger_tab_key(tab)
    if shared in pinned:
        return shared
    return role if role in pinned else None


def trigger_tabs(triggers: Iterable[TriggerEntry]) -> tuple[str, ...]:
    """The tabs the ``triggers`` name, each once, in their order."""
    return tuple(dict.fromkeys(trigger.tab for trigger in triggers if trigger.tab is not None))


def tabbed_roles(triggers: Iterable[TriggerEntry]) -> frozenset[str]:
    """The roles whose agents start only from the tabbed ones of ``triggers``: those a trigger
    with a tab queues and none without one does."""
    listed = tuple(triggers)
    tabbed = _queued_roles(trigger for trigger in listed if trigger.tab is not None)
    untabbed = _queued_roles(trigger for trigger in listed if trigger.tab is None)
    return frozenset(tabbed - untabbed)


def _queued_roles(triggers: Iterable[TriggerEntry]) -> frozenset[str]:
    """The roles the ``triggers`` queue tasks for."""
    return frozenset(role for trigger in triggers for role in trigger.roles)


def pin_trigger_tabs(
    tabs: tuple[ConversationTab, ...], triggers: Sequence[TriggerEntry]
) -> tuple[ConversationTab, ...]:
    """``tabs`` with a pinned tab for each tab the catalog's ``triggers`` name: the built-in
    roles' tabs first, but those of roles whose agents start only from tabbed triggers, unless
    they're conversational; then the triggers' tabs, in their order, those already pinned kept
    as they are; then the other conversational roles' and the agents' own tabs, as they are."""
    kept = {tab.key: tab for tab in tabs if tab.pinned}
    shared = (_trigger_tab(kept, name) for name in trigger_tabs(triggers))
    built_in = _built_in_tabs(kept, tabbed_roles(triggers))
    return (*built_in, *shared, *_other_conversations(tabs), *_unpinned(tabs))


def _built_in_tabs(
    kept: Mapping[str, ConversationTab], dropped: Collection[str]
) -> Iterator[ConversationTab]:
    """The built-in roles' pinned tabs, in order, each the one ``kept``, if any, but those of
    the ``dropped`` roles that aren't conversational."""
    built_in = (kept.get(tab.key, tab) for tab in PINNED_TABS)
    return (tab for tab in built_in if tab.lane is not None or tab.key not in dropped)


def _trigger_tab(kept: Mapping[str, ConversationTab], name: str) -> ConversationTab:
    """The pinned tab of the trigger's tab ``name``: the one ``kept``, if any, else a new one,
    labelled ``name``."""
    key = trigger_tab_key(name)
    return kept.get(key, ConversationTab(key, name, pinned=True))


def _other_conversations(tabs: Iterable[ConversationTab]) -> Iterator[ConversationTab]:
    """The pinned tabs among ``tabs`` of the conversational roles outside the built-in ones:
    those neither a built-in role's nor a trigger's tab."""
    return (
        tab
        for tab in tabs
        if tab.pinned and tab.key not in ROLE_TABS and not is_trigger_tab(tab.key)
    )


def pin_conversations(
    tabs: tuple[ConversationTab, ...], lanes: Mapping[str, BusyLane]
) -> tuple[ConversationTab, ...]:
    """``tabs`` with a pinned tab for each conversational role in ``lanes``, by role to the lane
    it answers in: the built-in roles' tabs first, each following its role's lane if it's among
    them, and the planner's its own lane if it isn't; then the triggers' tabs, as they are; then
    the other roles', in their order, those already pinned kept as they are and those no longer
    listed gone; then the agents' own tabs."""
    lanes = {PLANNER: PLANNER_LANE, **lanes}
    built_in = (replace(tab, lane=lanes.get(tab.key)) for tab in tabs if tab.key in ROLE_TABS)
    shared = (tab for tab in tabs if is_trigger_tab(tab.key))
    return (*built_in, *shared, *_pinned_others(tabs, lanes), *_unpinned(tabs))


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
    """Which of the ``pinned`` tabs each of the running ``agents`` writes into, by agent id, for
    those writing into one: its trigger's tab's, or its role's (see :func:`pinned_key`)."""
    keys = {agent.agent_id: pinned_key(agent.role, agent.tab, pinned) for agent in agents}
    return {agent: key for agent, key in keys.items() if key is not None}


@dataclass(frozen=True)
class TabbedAgent:
    """An agent at work in a trigger's tab: the tab's ``key`` and the agent's ``role``."""

    key: str
    role: str


@dataclass(frozen=True)
class Speakers:
    """The agents writing into the triggers' tabs: those ``at_work``, by agent id in the order
    they started, and the ``names`` labelling the entries of every one that has, by agent id,
    the last each had (see :func:`name_speakers`), so a late entry, such as its exit rule, keeps
    it."""

    at_work: Mapping[str, TabbedAgent] = field(default_factory=dict)
    names: Mapping[str, str] = field(default_factory=dict)


def name_speakers(at_work: Mapping[str, TabbedAgent]) -> dict[str, str]:
    """The name of each of the agents ``at_work``, by agent id in the order they started: its
    role, numbered from the second at work in that role in the same trigger's tab, as
    "bugs-reviewer 2", ….
    """
    seen: Counter[TabbedAgent] = Counter()
    names: dict[str, str] = {}
    for agent, tabbed in at_work.items():
        seen[tabbed] += 1
        count = seen[tabbed]
        names[agent] = tabbed.role if count == 1 else f"{tabbed.role} {count}"
    return names


def _seat_speakers(at_work: Mapping[str, TabbedAgent], names: Mapping[str, str]) -> Speakers:
    """The speakers with the agents ``at_work``, named afresh, and the ``names`` of the others."""
    return Speakers(at_work, {**names, **name_speakers(at_work)})


def recall_speakers(speakers: Speakers, names: Mapping[str, str]) -> Speakers:
    """``speakers`` naming agents by ``names`` too, those learnt from an earlier page of the
    log, but those at work by their own names."""
    return _seat_speakers(speakers.at_work, {**speakers.names, **names})


def running_speakers(
    agents: Iterable[RunningAgent],
    pinned: Collection[str],
    names: Mapping[str, str] = MappingProxyType({}),
) -> Speakers:
    """The speakers with the running ``agents`` writing into a trigger's tab among the
    ``pinned`` ones at work, in their order, keeping the ``names`` of those that wrote into one
    before."""
    at_work = {agent.agent_id: _tabbed(agent.role, agent.tab, pinned) for agent in agents}
    return _seat_speakers(_present(at_work), names)


def _tabbed(role: str, tab: str | None, pinned: Collection[str]) -> TabbedAgent | None:
    """An agent in ``role``, started by a trigger of the tab named ``tab``, at work in that
    trigger's tab, when it's among the ``pinned`` ones; None when it writes into no trigger's
    tab."""
    key = pinned_key(role, tab, pinned)
    return TabbedAgent(key, role) if key is not None and is_trigger_tab(key) else None


def _present(agents: Mapping[str, TabbedAgent | None]) -> dict[str, TabbedAgent]:
    """``agents`` but those at work in no trigger's tab."""
    return {agent: tabbed for agent, tabbed in agents.items() if tabbed is not None}


def follow_running(
    tabs: tuple[ConversationTab, ...], agents: Sequence[RunningAgent]
) -> tuple[ConversationTab, ...]:
    """``tabs`` agreeing with the ``agents`` running: each pinned tab counting the agents
    writing into it (a conversational role's following its busy lane), then an own tab for each
    agent writing into none, keeping those already open as they are."""
    return (*_count_pinned(tabs, agents), *_own_tabs(tabs, agents))


def _count_pinned(
    tabs: tuple[ConversationTab, ...], agents: Sequence[RunningAgent]
) -> tuple[ConversationTab, ...]:
    """``tabs``' pinned tabs, each counting the running ``agents`` writing into it but the
    conversational roles'."""
    keys = pinned_roles(tabs)
    counts = Counter(pinned_key(agent.role, agent.tab, keys) for agent in agents)
    pinned = (tab for tab in tabs if tab.pinned)
    return tuple(
        tab if tab.lane is not None else replace(tab, running=counts[tab.key]) for tab in pinned
    )


def _own_tabs(
    tabs: tuple[ConversationTab, ...], agents: Sequence[RunningAgent]
) -> tuple[ConversationTab, ...]:
    """The own tabs of the running ``agents`` writing into no pinned tab: those open in
    ``tabs``, then one for each of the rest."""
    own = _own_roles(agents, pinned_roles(tabs))
    kept = tuple(tab for tab in _unpinned(tabs) if tab.key in own)
    open_keys = {tab.key for tab in kept}
    added = (ConversationTab(key, role) for key, role in own.items() if key not in open_keys)
    return (*kept, *added)


def _unpinned(tabs: Iterable[ConversationTab]) -> Iterable[ConversationTab]:
    """``tabs`` but the pinned ones."""
    return (tab for tab in tabs if not tab.pinned)


def _own_roles(agents: Iterable[RunningAgent], pinned: Collection[str]) -> dict[str, str]:
    """The role of each of the running ``agents`` writing into none of the ``pinned`` tabs, by
    agent id."""
    return {
        agent.agent_id: agent.role
        for agent in agents
        if pinned_key(agent.role, agent.tab, pinned) is None
    }


def stopped_callers(
    events: Iterable[Event], agents: Iterable[RunningAgent]
) -> tuple[str | None, ...]:
    """The agents that start tool calls or requests to orbit in ``events`` but aren't among
    the ``agents`` running, so none of their calls can still run; the planner's agent is among
    those running from its first turn until the session ends."""
    running = {agent.agent_id for agent in agents}
    callers = (e.agent_id for e in events if isinstance(e, ToolCallStarted | AgentRequest))
    return tuple(dict.fromkeys(agent for agent in callers if agent not in running))


def unlogged_agents(
    agents: Iterable[RunningAgent], events: Iterable[Event]
) -> tuple[RunningAgent, ...]:
    """The running ``agents`` whose start isn't among ``events``, in their order."""
    logged = {event.agent_id for event in events if isinstance(event, AgentStarted)}
    return tuple(agent for agent in agents if agent.agent_id not in logged)


def track_roles(
    roles: Mapping[str, str], event: Event, pinned: Collection[str] = ROLE_TABS
) -> Mapping[str, str]:
    """Which of the ``pinned`` tabs each agent started writing into, by agent id, after
    ``event``: its trigger's tab's, or its role's (see :func:`pinned_key`)."""
    if not isinstance(event, AgentStarted):
        return roles
    key = pinned_key(event.role, event.tab, pinned)
    return roles if key is None else {**roles, event.agent_id: key}


def track_speakers(speakers: Speakers, event: Event, pinned: Collection[str]) -> Speakers:
    """``speakers`` after ``event``: an agent starting to write into a trigger's tab among the
    ``pinned`` ones at work after the others, one starting anywhere else nameless, and one
    exiting no longer at work, keeping its name."""
    if isinstance(event, AgentStarted):
        return _start_speaker(speakers, event, pinned)
    if isinstance(event, AgentExited) and event.agent_id in speakers.at_work:
        return _seat_speakers(_without(speakers.at_work, event.agent_id), speakers.names)
    return speakers


def _start_speaker(speakers: Speakers, event: AgentStarted, pinned: Collection[str]) -> Speakers:
    """``speakers`` with ``event``'s agent at work after the others if it writes into a
    trigger's tab among the ``pinned`` ones, else neither at work nor named."""
    agent = event.agent_id
    at_work = _present(
        {**_without(speakers.at_work, agent), agent: _tabbed(event.role, event.tab, pinned)}
    )
    return _seat_speakers(at_work, _without(speakers.names, agent))


def _without[T](mapping: Mapping[str, T], key: str) -> dict[str, T]:
    """``mapping`` without ``key``."""
    return {other: value for other, value in mapping.items() if other != key}


def tab_key(agent: str | None, roles: Mapping[str, str]) -> str | None:
    """Which tab ``agent``'s entries belong to: the pinned one ``roles`` has it writing into,
    its trigger's tab or its role's, else its own."""
    return None if agent is None else roles.get(agent, agent)


@dataclass(frozen=True)
class TriggerTab:
    """The ``tab`` a trigger's agents share, and the ``roles`` of the tasks it queues, the only
    ones whose agents and notes go under it."""

    tab: str
    roles: tuple[str, ...]


def tabs_by_trigger(triggers: Iterable[TriggerEntry]) -> Mapping[str, TriggerTab]:
    """The tabs of the tabbed ones of ``triggers``, with the roles they queue, by trigger
    name."""
    return {
        trigger.name: TriggerTab(trigger.tab, trigger.roles)
        for trigger in triggers
        if trigger.tab is not None
    }


def task_tab_key(
    state: TaskState | None, triggers: Mapping[str, TriggerTab], pinned: Collection[str]
) -> str | None:
    """Which of the ``pinned`` tabs the notes on a task (its header, verification results and
    merge notes) belong to: the one its agent writes into (see :func:`pinned_key`), its
    trigger's tab's, by its origin among the tabs of ``triggers`` by name, if that trigger
    queues the task's role, or its role's. None, for All only, when it writes into neither or
    the task isn't on the list (``state`` None)."""
    if state is None:
        return None
    return pinned_key(state.task.role, _task_trigger_tab(state, triggers), pinned)


def _task_trigger_tab(state: TaskState, triggers: Mapping[str, TriggerTab]) -> str | None:
    """The tab of the trigger among ``triggers`` that ``state``'s origin names, if it queues the
    task's role, as a merge pass doesn't its fix coders."""
    trigger = triggers.get(state.origin)
    return None if trigger is None or state.task.role not in trigger.roles else trigger.tab


def track_tabs(
    tabs: tuple[ConversationTab, ...],
    event: Event,
    active: str | None = None,
    roles: Mapping[str, str] = MappingProxyType({}),
) -> tuple[ConversationTab, ...]:
    """The open conversation tabs after ``event``; output for a tab other than ``active``,
    the one being shown, marks it unread. Agents in a pinned role or a trigger's tab share it,
    and ``roles`` says which tab those agents write into."""
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
    work on the pinned tab ``roles`` has ``agent`` writing into, if any."""
    tabs = tuple(tab for tab in tabs if tab.pinned or tab.key != agent)
    return _count_running(tabs, roles.get(agent), -1)


def _is_tab_output(event: Event) -> TypeGuard[_TabOutput]:
    """Whether ``event`` adds an entry to its agent's conversation."""
    return isinstance(event, _TAB_OUTPUT)


def _open_tab(
    tabs: tuple[ConversationTab, ...], event: AgentStarted
) -> tuple[ConversationTab, ...]:
    """``tabs`` with one for the started agent, unless its trigger's tab or its role has a
    pinned tab, which counts one more agent at work instead."""
    key = pinned_key(event.role, event.tab, pinned_roles(tabs))
    if key is not None:
        return _count_running(tabs, key, 1)
    return (*tabs, ConversationTab(event.agent_id, event.role))


def _count_running(
    tabs: tuple[ConversationTab, ...], role: str | None, change: int
) -> tuple[ConversationTab, ...]:
    """``tabs`` with ``change`` more agents at work on the pinned tab keyed ``role``, a role's or
    a trigger's tab's, which is None for an agent with no pinned tab. A conversational role's tab
    is left to ``mark_lane_busy``."""
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
