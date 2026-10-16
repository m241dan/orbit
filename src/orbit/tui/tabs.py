"""The strip of conversation tabs above the transcript."""

from collections.abc import Mapping
from typing import Any

from textual.widgets import Tab, Tabs

from orbit.protocol import Event
from orbit.tui.view import (
    ALL_TAB_LABEL,
    PINNED_TABS,
    PLANNER,
    ConversationTab,
    mark_planner_busy,
    mark_tab_read,
    mark_tab_unread,
    tab_key,
    tab_labels,
    track_roles,
    track_tabs,
)


class ConversationTabs(Tabs):
    """Strip of tabs above the transcript: "All", a pinned tab per built-in role, then one per
    running agent in any other role.

    Clicking a tab activates it; like the transcript, it never takes focus.
    """

    can_focus = False

    ALL_TAB_ID = "tab-all"
    PLANNER_TAB_ID = f"tab-{PLANNER}"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            Tab(ALL_TAB_LABEL, id=self.ALL_TAB_ID),
            *(Tab(tab.role, id=f"tab-{tab.key}") for tab in PINNED_TABS),
            **kwargs,
        )
        self.tabs: tuple[ConversationTab, ...] = PINNED_TABS
        self.roles: Mapping[str, str] = {}  # Agents that ran in a pinned role, to the role.
        # Tab key (an agent id, or a pinned role's) to its tab's id.
        self._tab_ids: dict[str, str] = {tab.key: f"tab-{tab.key}" for tab in PINNED_TABS}
        self._next_id = 1
        self._before_planner = self.ALL_TAB_ID  # Where ctrl+t goes back to from the planner.

    def agent_of(self, tab_id: str) -> str | None:
        """The key of the tab with id ``tab_id``: an agent's id, a pinned role's, or None for
        All."""
        return next((agent for agent, tid in self._tab_ids.items() if tid == tab_id), None)

    def key_of(self, agent: str | None) -> str | None:
        """The key of the tab ``agent``'s entries belong to."""
        return tab_key(agent, self.roles)

    def toggle_planner(self) -> None:
        """Show the planner tab, or from there, the tab shown before it (All if it's gone)."""
        if self.active != self.PLANNER_TAB_ID:
            self._before_planner = self.active
            self.active = self.PLANNER_TAB_ID
        else:
            # Back to the tab we came from (All included), or All if that tab has since closed.
            open_tabs = {self.ALL_TAB_ID, *self._tab_ids.values()}
            back = self._before_planner
            self.active = back if back in open_tabs else self.ALL_TAB_ID

    def track(self, event: Event) -> None:
        """Add a tab for an agent that started outside the pinned roles, remove it when that
        agent exits, and mark a tab not being shown when one of its agents has new output."""
        self.roles = track_roles(self.roles, event)
        self._update(track_tabs(self.tabs, event, self.agent_of(self.active), self.roles))

    def mark_read(self, agent: str | None) -> None:
        """Clear ``agent``'s unread marker; None (All) has none."""
        if agent is not None:
            self._update(mark_tab_read(self.tabs, agent))

    def mark_unread(self, key: str | None) -> None:
        """Mark the tab keyed ``key`` unread, as an entry with no agent landed in it, unless
        it's being shown; None (All) has no marker."""
        self._update(mark_tab_unread(self.tabs, key, self.agent_of(self.active)))

    def set_planner_busy(self, busy: bool) -> None:
        """Show the planner tab's running marker while the planner is ``busy`` answering."""
        self._update(mark_planner_busy(self.tabs, busy))

    def _update(self, tabs: tuple[ConversationTab, ...]) -> None:
        if tabs == self.tabs:
            return
        for gone in set(self._tab_ids) - {t.key for t in tabs}:
            self._remove(gone)
        for tab, label in zip(tabs, tab_labels(tabs), strict=True):
            if tab.key in self._tab_ids:
                self._relabel(self._tab_ids[tab.key], label)
            else:
                self._add(tab.key, label)
        self.tabs = tabs

    def _add(self, agent: str, label: str) -> None:
        tab_id = f"agent-tab-{self._next_id}"
        self._next_id += 1
        self._tab_ids[agent] = tab_id
        self.add_tab(Tab(label, id=tab_id))

    def _remove(self, agent: str) -> None:
        """Remove ``agent``'s tab, showing All instead if it was active."""
        tab_id = self._tab_ids.pop(agent)
        if self.active == tab_id:
            self.active = self.ALL_TAB_ID
        self.remove_tab(tab_id)

    def _relabel(self, tab_id: str, label: str) -> None:
        tab = self.query_one(f"#{tab_id}", Tab)
        if tab.label_text != label:
            tab.label = label
            if self.active == tab_id:
                # Let the transcript's title follow the new label.
                self.post_message(self.TabActivated(self, tab))
