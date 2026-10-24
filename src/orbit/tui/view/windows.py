"""Which window h/j/k/l reaches from each window."""

from typing import Protocol

NEIGHBORS: dict[str, dict[str, str]] = {
    "transcript": {"j": "prompt", "l": "task-list"},
    "detail": {"k": "transcript", "j": "prompt", "l": "task-list"},
    "prompt": {"k": "transcript", "l": "verification-steps"},
    "task-list": {"h": "transcript", "j": "verification-steps"},
    "verification-steps": {"h": "prompt", "k": "task-list", "j": "agents"},
    "agents": {"h": "prompt", "k": "verification-steps"},
}
"""The window reached from each window by h/j/k/l; missing means an edge. Conversation over
prompt on the left, task list over verification steps on the right; the conversation's detail
pane, while it's open, between the conversation and the prompt."""

OPEN_DETAIL_NEIGHBORS: dict[str, str] = {"j": "detail"}
"""The conversation's moves that differ while its detail pane is open."""

SIDEBAR_WINDOWS = frozenset({"task-list", "verification-steps", "agents"})
"""The windows in the sidebar, which h/j/k/l skip while ctrl+b hides it."""


def neighbors(selected: str, detail_open: bool, sidebar_shown: bool) -> dict[str, str]:
    """The windows h/j/k/l reach from ``selected``, given whether the conversation's detail pane
    is open (``detail_open``) and the sidebar is shown (``sidebar_shown``)."""
    moves = NEIGHBORS[selected]
    if selected == "transcript" and detail_open:
        moves = moves | OPEN_DETAIL_NEIGHBORS
    if sidebar_shown:
        return moves
    return {key: window for key, window in moves.items() if window not in SIDEBAR_WINDOWS}


class WindowsView(Protocol):
    """Where :class:`Windows` mark the window selected."""

    def mark_selected(self, window: str, selected: bool) -> None:
        """Mark ``window`` as ``selected``, or no longer."""


class Windows:
    """The window h/j/k/l last moved to, marked in ``view``: the prompt, to begin with."""

    def __init__(self, view: WindowsView) -> None:
        self._view = view
        self.selected = "prompt"

    def start(self) -> None:
        """Mark the window selected."""
        self._view.mark_selected(self.selected, True)

    def move(self, direction: str, detail_open: bool, sidebar_shown: bool) -> None:
        """Select the window ``direction`` reaches, if any, as :func:`neighbors` says given
        whether the detail pane is open (``detail_open``) and the sidebar shown
        (``sidebar_shown``)."""
        target = neighbors(self.selected, detail_open, sidebar_shown).get(direction)
        if target is not None:
            self.select(target)

    def select(self, window: str) -> None:
        """Select ``window`` in place of the window selected."""
        self._view.mark_selected(self.selected, False)
        self._view.mark_selected(window, True)
        self.selected = window
