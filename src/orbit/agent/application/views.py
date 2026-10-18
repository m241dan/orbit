"""Attaching connections to sessions: to the workspace's open one, or to another as a read-only
view, and what a connection viewing one may do there."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from orbit.agent.application.connection import Connections, SessionConnection
from orbit.agent.application.keeper import SessionKeeper
from orbit.agent.application.outbox import Outbox
from orbit.agent.application.session_state import SessionStates
from orbit.agent.application.settings import SessionSettings
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.sessions import (
    SessionRecord,
    not_active_message,
    summarize,
    viewed_refusal,
)
from orbit.protocol import (
    NOT_ACTIVE,
    PLANNER_LANE,
    WORK_LANE,
    WRONG_SESSION,
    ActivateSession,
    Attach,
    Command,
    DeleteSession,
    History,
    NewSession,
    RenameSession,
    SessionRef,
    Snapshot,
)

VIEW_COMMANDS = (Attach, ActivateSession, NewSession, History, RenameSession, DeleteSession)
"""The commands a connection viewing a session that isn't open may send; the others are
refused as ``not_active``."""


@dataclass(frozen=True)
class ViewParts:
    """What attaching uses: the ``keeper`` of the open session and the saved ones, the
    ``events`` it publishes into and reads the logged ones back from, the ``connections``, the
    sessions' ``states``, the ``settings``, the ``live`` snapshot of the open session, and
    whether the orbit is ``read_only``."""

    keeper: SessionKeeper
    events: Outbox
    connections: Connections
    states: SessionStates
    settings: SessionSettings
    live: Callable[[], Awaitable[Snapshot]]
    read_only: Callable[[], bool]


class SessionViews:
    """Attaches connections to the sessions in ``parts``: the open one, live, or a saved one as a
    read-only view, which runs nothing and publishes no events while it isn't open."""

    def __init__(self, parts: ViewParts) -> None:
        self._parts = parts

    async def attach(self, connection: SessionConnection, session_id: str) -> None:
        """Have ``connection`` view the session ``session_id`` and publish its snapshot to it:
        the open session's as it stands, or a saved one's from its record and its last logged
        events. Refused (raising :class:`Refused`) for a session there's none of or that's
        unreadable; nothing changes if the saved sessions can't be listed, which is reported."""
        parts = self._parts
        if session_id == parts.keeper.id:
            connection.attach(None)
            await parts.events.put(await parts.live())
            return
        record = await parts.keeper.saved(session_id)
        if record is None:
            return
        connection.attach(SessionRef(id=record.id, name=record.name))
        await connection.publish_here(await self._view(record))
        parts.states.check()

    def switching(self, kept: bool) -> None:
        """Have the connection whose command switches the open session view the session it
        switches to, and the others viewing the open session keep viewing it, if it's ``kept``
        saved, as a read-only view."""
        self._parts.connections.activate(self.open_session() if kept else None)

    async def activated(self, left: str) -> None:
        """Publish its snapshot to each connection, but the one asking, attached to the session
        ``left`` or to the one open in its place: the read-only view of the one, the other
        live. Nothing if ``left`` is still open, or if it can't be read, which is reported."""
        parts = self._parts
        if left == parts.keeper.id:
            return
        for connection in parts.connections.others_attached(parts.keeper.id):
            await connection.publish_here(await parts.live())
        viewers = parts.connections.others_attached(left)
        record = await parts.keeper.saved(left) if viewers else None
        if record is None:
            return
        view = await self._view(record)
        for connection in viewers:
            await connection.publish_here(view)

    def open_session(self) -> SessionRef:
        """The workspace's open session."""
        keeper = self._parts.keeper
        return SessionRef(id=keeper.id, name=keeper.name)

    def check_addressed(self, connection: SessionConnection, session: str | None) -> None:
        """Refuse a command from ``connection`` for ``session`` unless that's the session it
        views or none, which means that one."""
        viewing = connection.viewing
        viewed = self._parts.keeper.id if viewing is None else viewing.id
        if session is None or session == viewed:
            return
        if viewing is None:
            message = f"Session {session!r} isn't open; the open session is {viewed}."
        else:
            message = f"Session {session!r} isn't the one viewed; this connection views {viewed}."
        raise Refused(WRONG_SESSION, message)

    def admit(self, connection: SessionConnection, command: Command) -> None:
        """Refuse ``command`` from ``connection`` as ``not_active`` if it views a session that
        isn't open, unless it's one of the :data:`VIEW_COMMANDS`."""
        viewing = connection.viewing
        if viewing is None or isinstance(command, VIEW_COMMANDS):
            return
        raise Refused(NOT_ACTIVE, not_active_message(viewing.name, self._parts.keeper.name))

    async def check_deletable(self, session_id: str) -> None:
        """Refuse deleting the session ``session_id`` as ``busy`` while connections other than
        the one asking view it; the open session is left for the keeper to refuse."""
        parts = self._parts
        viewers = parts.connections.others_viewing(session_id)
        if session_id == parts.keeper.id or viewers == 0:
            return
        summaries = await parts.keeper.summaries()
        name = next((s.name for s in summaries if s.id == session_id), session_id)
        raise viewed_refusal(name, viewers)

    async def _view(self, record: SessionRecord) -> Snapshot:
        """The read-only view of the session saved as ``record``: as it was saved, with its last
        logged events, nothing running in it."""
        parts = self._parts
        transcript = await parts.events.transcript(record.id)
        return Snapshot(
            session=summarize(record),
            settings=record.settings,
            model_choices=parts.settings.model_choices,
            busy={WORK_LANE: False, PLANNER_LANE: False},
            tasks=record.tasks,
            steps=(),
            review=None,
            proposals=(),
            agents=(),
            transcript=transcript,
            read_only=parts.read_only(),
            active=False,
            open_session=self.open_session(),
            viewers=parts.states.of(record.id).viewers,
        )
