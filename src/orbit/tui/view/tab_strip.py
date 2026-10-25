"""The strip of conversation tabs, without the widget showing it: the tabs it has, labelled and
marked, in order, and how the user moves between them."""

from collections.abc import Iterable, Mapping, Sequence
from typing import Protocol

from orbit.protocol import PLANNER, BusyLane, Event, RunningAgent
from orbit.tui.view.tabs import (
    ALL_TAB_LABEL,
    PINNED_TABS,
    ConversationTab,
    follow_running,
    mark_lane_busy,
    mark_tab_read,
    mark_tab_unread,
    pin_conversations,
    pinned_roles,
    replay_tabs,
    reset_tabs,
    running_roles,
    tab_key,
    tab_labels,
    track_roles,
    track_tabs,
)

ALL_TAB_ID = "tab-all"
PLANNER_TAB_ID = f"tab-{PLANNER}"
OPENING: tuple[tuple[str, str], ...] = (
    (ALL_TAB_ID, ALL_TAB_LABEL),
    *((f"tab-{tab.key}", tab.role) for tab in PINNED_TABS),
)
"""The tabs the strip starts with, by id and label: All, then a pinned tab per built-in role."""


class TabStripView(Protocol):
    """Where a :class:`TabStrip` shows, which holds the active tab, as the user may click
    another."""

    @property
    def active(self) -> str:
        """The id of the active tab."""
        ...

    def activate(self, tab_id: str) -> None:
        """Make the tab ``tab_id`` the active one, which then shows."""

    def open_tab(self, tab_id: str, label: str, before: str | None = None) -> None:
        """Add the tab ``tab_id``, labelled ``label``, before the tab ``before``, or at the end
        of the strip without one."""

    def close_tab(self, tab_id: str) -> None:
        """Remove the tab ``tab_id``."""

    def label_tab(self, tab_id: str, label: str) -> None:
        """Label the tab ``tab_id`` ``label``."""

    def relabelled(self, key: str | None, label: str) -> None:
        """Say the active tab, keyed ``key``, is now labelled ``label``."""


class TabStrip:
    """The tabs above the conversation, shown in ``view``: "All", a pinned tab per built-in
    role, then one per other conversational role the catalog lists, then one per running agent
    in any other role."""

    def __init__(self, view: TabStripView) -> None:
        self._view = view
        self.tabs: tuple[ConversationTab, ...] = PINNED_TABS
        self.roles: Mapping[str, str] = {}  # Agents that ran in a pinned role, to the role.
        # Tab key (an agent id, or a pinned role's) to its tab's id.
        self._tab_ids: dict[str, str] = {tab.key: f"tab-{tab.key}" for tab in PINNED_TABS}
        self._labels: dict[str, str] = dict(OPENING)  # Each tab's label, by its id.
        self._order = [tab_id for tab_id, _ in OPENING]  # The tabs' ids, as the strip has them.
        self._next_id = 1
        # Where ctrl+t goes back to from the last conversational role's tab.
        self._before_conversations = ALL_TAB_ID

    def agent_of(self, tab_id: str) -> str | None:
        """The key of the tab with id ``tab_id``: an agent's id, a pinned role's, or None for
        All."""
        return next((agent for agent, tid in self._tab_ids.items() if tid == tab_id), None)

    def label_of(self, tab_id: str) -> str:
        """The label of the tab with id ``tab_id``."""
        return self._labels[tab_id]

    def key_of(self, agent: str | None) -> str | None:
        """The key of the tab ``agent``'s entries belong to."""
        return tab_key(agent, self.roles)

    def cycle_conversations(self) -> None:
        """Show the first conversational role's tab (the planner's), from there each next one,
        and from the last, the tab shown before the first (All if it's gone)."""
        ids = [self._tab_ids[tab.key] for tab in self.tabs if tab.lane is not None]
        active = self._view.active
        if active not in ids:
            self._before_conversations = active
            self._view.activate(ids[0])
            return
        following = ids.index(active) + 1
        if following < len(ids):
            self._view.activate(ids[following])
            return
        # Back to the tab we came from (All included), or All if that tab has since closed.
        back = self._before_conversations
        self._view.activate(back if back in self._order else ALL_TAB_ID)

    def cycle(self, step: int) -> None:
        """Show the next tab, or with a negative ``step`` the one before, wrapping round; with
        none active, the first or the last."""
        order, active = self._order, self._view.active
        direction = 1 if step > 0 else -1
        if not active:
            self._view.activate(order[0 if direction == 1 else -1])
            return
        self._view.activate(order[(order.index(active) + direction) % len(order)])

    def pin(self, lanes: Mapping[str, BusyLane]) -> None:
        """Pin a tab for each conversational role in ``lanes``, by role to the lane it answers
        in, after the built-in roles' tabs, closing those of roles no longer listed."""
        self._update(pin_conversations(self.tabs, lanes))

    def track(self, event: Event) -> None:
        """Add a tab for an agent that started outside the pinned roles, remove it when that
        agent exits, and mark a tab not being shown when one of its agents has new output."""
        self.learn_roles(event)
        self._update(track_tabs(self.tabs, event, self.agent_of(self._view.active), self.roles))

    def replay(self, event: Event) -> None:
        """Add and remove agents' tabs as :meth:`track` does for ``event``, one replayed from
        the session's log, marking none unread."""
        self.learn_roles(event)
        self._update(replay_tabs(self.tabs, event, self.roles))

    def learn_roles(self, event: Event) -> None:
        """Note the pinned role an agent started in by ``event``, an earlier one, leaving the
        tabs as they are."""
        self.roles = track_roles(self.roles, event, pinned_roles(self.tabs))

    def reset(self, running: Sequence[RunningAgent]) -> None:
        """Close every agent's own tab and forget the agents that ran in the pinned roles,
        as before any agent ran, but those ``running`` in one."""
        self.roles = running_roles(running, pinned_roles(self.tabs))
        self._update(reset_tabs(self.tabs))

    def follow(self, running: Sequence[RunningAgent]) -> None:
        """Make the tabs agree with the agents ``running``: an own tab for each outside the
        pinned roles, and each pinned tab marked while its role's agents run."""
        self.roles = {**self.roles, **running_roles(running, pinned_roles(self.tabs))}
        self._update(follow_running(self.tabs, running))

    def mark_read(self, agent: str | None) -> None:
        """Clear ``agent``'s unread marker; None (All) has none."""
        if agent is not None:
            self._update(mark_tab_read(self.tabs, agent))

    def mark_unread(self, key: str | None) -> None:
        """Mark the tab keyed ``key`` unread, as an entry with no agent landed in it, unless
        it's being shown; None (All) has no marker."""
        self._update(mark_tab_unread(self.tabs, key, self.agent_of(self._view.active)))

    def set_busy(self, lane: BusyLane, busy: bool) -> None:
        """Show the running marker of the tabs of the conversational roles answering in
        ``lane`` while it's ``busy``."""
        self._update(mark_lane_busy(self.tabs, lane, busy))

    def _update(self, tabs: tuple[ConversationTab, ...]) -> None:
        if tabs == self.tabs:
            return
        for gone in set(self._tab_ids) - {t.key for t in tabs}:
            self._remove(gone)
        for index, (tab, label) in enumerate(zip(tabs, tab_labels(tabs), strict=True)):
            if tab.key in self._tab_ids:
                self._relabel(self._tab_ids[tab.key], label)
            else:
                self._add(tab.key, label, self._first_open(tabs[index + 1 :]))
        self.tabs = tabs

    def _first_open(self, tabs: Iterable[ConversationTab]) -> str | None:
        """The id of the first of ``tabs`` the strip has open; none if it has none of them."""
        return next((self._tab_ids[tab.key] for tab in tabs if tab.key in self._tab_ids), None)

    def _add(self, key: str, label: str, before: str | None) -> None:
        """Open the tab keyed ``key``, labelled ``label``, before the tab ``before``, or at the
        end without one."""
        tab_id = f"agent-tab-{self._next_id}"
        self._next_id += 1
        self._tab_ids[key] = tab_id
        self._labels[tab_id] = label
        at = len(self._order) if before is None else self._order.index(before)
        self._order.insert(at, tab_id)
        self._view.open_tab(tab_id, label, before)

    def _remove(self, agent: str) -> None:
        """Remove ``agent``'s tab, showing All instead if it was active."""
        tab_id = self._tab_ids.pop(agent)
        if self._view.active == tab_id:
            self._view.activate(ALL_TAB_ID)
        del self._labels[tab_id]
        self._order.remove(tab_id)
        self._view.close_tab(tab_id)

    def _relabel(self, tab_id: str, label: str) -> None:
        if self._labels[tab_id] != label:
            self._labels[tab_id] = label
            self._view.label_tab(tab_id, label)
            if self._view.active == tab_id:
                self._view.relabelled(self.agent_of(tab_id), label)
