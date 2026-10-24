"""The strip of conversation tabs above the transcript."""

from typing import Any

from textual.message import Message
from textual.widgets import Tab, Tabs

from orbit.tui.view.tab_strip import OPENING, TabStrip


class ConversationTabs(Tabs):
    """Strip of tabs above the transcript, as its :class:`TabStrip` has them.

    Clicking a tab activates it; like the transcript, it never takes focus.
    """

    can_focus = False

    class Relabelled(Message):
        """The shown tab, keyed ``key``, is now labelled ``label``."""

        def __init__(self, key: str | None, label: str) -> None:
            super().__init__()
            self.key = key
            self.label = label

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(*(Tab(label, id=tab_id) for tab_id, label in OPENING), **kwargs)
        self.strip = TabStrip(self)

    def activate(self, tab_id: str) -> None:
        self.active = tab_id

    def open_tab(self, tab_id: str, label: str) -> None:
        self.add_tab(Tab(label, id=tab_id))

    def close_tab(self, tab_id: str) -> None:
        self.remove_tab(tab_id)

    def label_tab(self, tab_id: str, label: str) -> None:
        self.query_one(f"#{tab_id}", Tab).label = label

    def relabelled(self, key: str | None, label: str) -> None:
        self.post_message(self.Relabelled(key, label))
