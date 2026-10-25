"""Each session's state as connections see it: whether it's the open one, whether it's running
and what's in progress in it, until when and why its run is paused, and how many connections
view it, published whenever any of these changes."""

from collections.abc import Callable
from dataclasses import dataclass

from orbit.agent.application.connection import Connections
from orbit.agent.domain.activity import Activity, reported
from orbit.agent.domain.usage_limits import Paused
from orbit.protocol import Event, SessionStateChanged


@dataclass(frozen=True)
class StateParts:
    """What the state of the sessions is read from: the ``open_session``'s id, the ``activity``
    in progress there, the ``connections`` viewing the sessions, ``post``, which publishes an
    event without blocking, and how its run is ``paused``, if it is."""

    open_session: Callable[[], str]
    activity: Callable[[], Activity]
    connections: Connections
    post: Callable[[Event], None]
    paused: Callable[[], Paused | None]


def _unviewed(session_id: str) -> SessionStateChanged:
    """The state of the session ``session_id`` that isn't open and no connection views."""
    return SessionStateChanged(session=session_id, active=False, state="idle", viewers=0)


def _open(
    session_id: str, viewers: int, activity: Activity, paused: Paused | None
) -> SessionStateChanged:
    """The state of the open session ``session_id``, which ``viewers`` connections view, with
    ``activity`` in progress, its run ``paused`` so, if it is."""
    return SessionStateChanged(
        session=session_id,
        active=True,
        state="running" if activity.running else "idle",
        viewers=viewers,
        activity=reported(activity),
        paused_until=paused.until if paused else None,
        paused_for=paused.reason if paused else None,
    )


class SessionStates:
    """Publishes ``SessionStateChanged`` for a session in ``parts`` whenever whether it's the
    open one, whether it's running, what's in progress in it, how its run is paused or how many
    connections view it changes: the open session, and those connections view, and any that was
    and no longer is either."""

    def __init__(self, parts: StateParts) -> None:
        self._parts = parts
        self._last = self.current()  # As last published, or as it was to begin with.

    def current(self, *, joining: bool = False) -> dict[str, SessionStateChanged]:
        """The state now of the open session, last, and of those connections view before it, by
        id, counting, if ``joining``, the connection whose ``Hello`` is being answered among the
        open session's viewers."""
        parts = self._parts
        open_id = parts.open_session()
        viewed = parts.connections.viewed(joining=joining)
        states = {
            session_id: _unviewed(session_id).model_copy(update={"viewers": viewers})
            for session_id, viewers in viewed.items()
            if session_id != open_id
        }
        states[open_id] = _open(open_id, viewed[open_id], parts.activity(), parts.paused())
        return states

    def of(self, session_id: str) -> SessionStateChanged:
        """The state now of the session ``session_id``, counting the connection whose command
        is being answered among its viewers if it views it."""
        return self.current(joining=True).get(session_id) or _unviewed(session_id)

    def check(self) -> None:
        """Publish the state of each session whose state changed since it was last published:
        first those no longer open nor viewed, then those viewed, then the open session."""
        now = self.current()
        last, self._last = self._last, now
        left = [_unviewed(session_id) for session_id in last if session_id not in now]
        for state in (*left, *now.values()):
            if last.get(state.session) != state:
                self._parts.post(state)
