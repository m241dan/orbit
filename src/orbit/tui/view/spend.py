"""What the session spent, by window: the transcript's corner and the breakdown by role."""

from collections.abc import Callable, Mapping
from typing import Protocol

from orbit.protocol import Event, SessionStateChanged, Snapshot, Usage
from orbit.tui.view.common import counted
from orbit.tui.view.tasks import NO_COST, cost_text

NAMED_CONSULTANTS = 2
"""The most consultants a role's corner names; beyond, it counts them."""

SPENDING_TITLE = "Usage by role"
SPENDING_HINT = "j/k scroll · esc close"
NO_SPENDING = "Nothing spent yet."

type RoleSpent = Mapping[str, Usage]
type CausedSpent = Mapping[str, Mapping[str, Usage]]


def _cost(usage: Usage) -> str:
    return cost_text(usage) or NO_COST


def _total(usages: list[Usage]) -> Usage:
    return sum(usages, Usage())


def role_corner(role: str, role_spent: RoleSpent, caused_spent: CausedSpent) -> str:
    """The corner of ``role``'s tab: what it spent itself (``role_spent``), then what it caused
    each consultant to spend (``caused_spent``), named while there are at most
    :data:`NAMED_CONSULTANTS`, and the sum: ``planner $0.42 · via experimenter $1.10 · $1.52``;
    just its own figure when it caused nothing, and empty when it spent nothing."""
    own = role_spent.get(role, Usage())
    caused = caused_spent.get(role, {})
    if not caused:
        return cost_text(own)
    return f"{role} {_cost(own)} · {_via(caused)} · {_cost(own + _total(list(caused.values())))}"


def _via(caused: Mapping[str, Usage]) -> str:
    if len(caused) > NAMED_CONSULTANTS:
        sum_ = _total(list(caused.values()))
        return f"via {counted(len(caused), 'consultant')} {_cost(sum_)}"
    return "via " + ", ".join(f"{name} {_cost(usage)}" for name, usage in caused.items())


def session_corner(usage: Usage) -> str:
    """The All tab's corner: the session's total, ``usage``; empty when it spent nothing."""
    cost = cost_text(usage)
    return f"session {cost}" if cost else ""


def spending_text(role_spent: RoleSpent, caused_spent: CausedSpent) -> str:
    """The breakdown by role: for each role with usage, what it spent itself and, a line each,
    what it caused each consultant to spend."""
    roles = dict.fromkeys([*role_spent, *caused_spent])
    lines = [line for role in roles for line in _role_lines(role, role_spent, caused_spent)]
    return "\n".join(lines) or NO_SPENDING


def _role_lines(role: str, role_spent: RoleSpent, caused_spent: CausedSpent) -> list[str]:
    caused = caused_spent.get(role, {})
    own = f"{role}: {_cost(role_spent.get(role, Usage()))} itself"
    return [own, *(f"  caused {name}: {_cost(usage)}" for name, usage in caused.items())]


class CornerView(Protocol):
    """Where the shown tab's usage goes: the transcript's corner."""

    def show_corner(self, text: str) -> None:
        """Show ``text`` in the corner."""
        ...


class UsageCorner:
    """The shown tab's usage, in ``view``: a conversational role's own and caused spend, a task
    agent's task's usage (``task_usage``, by agent id) or, on All, the session's total
    (``session_usage``). It keeps what the session viewed spent by role, as its snapshot and
    state changes say."""

    def __init__(
        self,
        view: CornerView,
        session_usage: Callable[[], Usage],
        task_usage: Callable[[str], Usage | None],
    ) -> None:
        self._view = view
        self._session_usage = session_usage
        self._task_usage = task_usage
        self._session: str | None = None
        self.role_spent: RoleSpent = {}
        self.caused_spent: CausedSpent = {}
        self._key: str | None = None
        self._conversational = False

    def show_tab(self, key: str | None, conversational: bool) -> None:
        """Show the usage of the tab keyed ``key``, a ``conversational`` role's or not."""
        self._key = key
        self._conversational = conversational
        self.show()

    @property
    def conversational(self) -> bool:
        """Whether the tab shown is a conversational role's."""
        return self._conversational

    def track(self, event: Event) -> None:
        """Follow what the session viewed spent through ``event``, and show the corner again."""
        if isinstance(event, Snapshot):
            self._session = event.session.id
            self._spent(event.role_spent, event.caused_spent)
        elif isinstance(event, SessionStateChanged) and event.session == self._session:
            self._spent(
                event.role_spent if event.role_spent is not None else self.role_spent,
                event.caused_spent if event.caused_spent is not None else self.caused_spent,
            )
        self.show()

    def _spent(self, role_spent: RoleSpent, caused_spent: CausedSpent) -> None:
        self.role_spent = role_spent
        self.caused_spent = caused_spent

    def show(self) -> None:
        """Show the shown tab's usage in the corner."""
        self._view.show_corner(self._text())

    def _text(self) -> str:
        key = self._key
        if key is None:
            return session_corner(self._session_usage())
        if self._conversational:
            return role_corner(key, self.role_spent, self.caused_spent)
        usage = self._task_usage(key)
        return "" if usage is None else cost_text(usage)

    def breakdown(self) -> str:
        """The breakdown by role of what the session viewed spent."""
        return spending_text(self.role_spent, self.caused_spent)
