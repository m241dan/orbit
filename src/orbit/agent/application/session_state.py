"""The open session's state as connections see it: whether it's running, and how many
connections view it, published whenever either changes."""

from collections.abc import Callable
from dataclasses import dataclass

from orbit.agent.application.connection import Connections
from orbit.protocol import Event, SessionStateChanged


@dataclass(frozen=True)
class StateParts:
    """What the state of the session open is read from: the ``open_session``'s id, whether it's
    ``running``, the ``connections`` viewing it, and ``post``, which publishes an event without
    blocking."""

    open_session: Callable[[], str]
    running: Callable[[], bool]
    connections: Connections
    post: Callable[[Event], None]


class SessionStates:
    """Publishes ``SessionStateChanged`` for the session open in ``parts`` whenever whether it's
    running or how many connections view it changes, and, when another session is opened in
    its place, for the session left too."""

    def __init__(self, parts: StateParts) -> None:
        self._parts = parts
        self._last = self.current()  # As last published, or as it was to begin with.

    def current(self, *, joining: bool = False) -> SessionStateChanged:
        """The open session's state now, counting, if ``joining``, the connection whose
        ``Hello`` is being answered among its viewers."""
        parts = self._parts
        return SessionStateChanged(
            session=parts.open_session(),
            active=True,
            state="running" if parts.running() else "idle",
            viewers=parts.connections.viewers(joining=joining),
        )

    def check(self) -> None:
        """Publish the open session's state if it changed since it was last published, after
        the state of the session it replaced, if it did."""
        now = self.current()
        last, self._last = self._last, now
        if now == last:
            return
        if last.session != now.session:
            self._parts.post(
                SessionStateChanged(session=last.session, active=False, state="idle", viewers=0)
            )
        self._parts.post(now)
