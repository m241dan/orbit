"""Attaching connections to sessions: to the workspace's open one, or to another as a read-only
view, and what a connection viewing one may do there."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import NotRequired, TypedDict, Unpack

from orbit.agent.application.connection import Connections, SessionConnection
from orbit.agent.application.keeper import SessionKeeper
from orbit.agent.application.outbox import Outbox, Shown
from orbit.agent.application.session_state import SessionStates
from orbit.agent.application.settings import SessionSettings
from orbit.agent.application.storage_halt import StorageHalt
from orbit.agent.application.takeover import Takeover
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.sessions import (
    SessionRecord,
    check_addressed_session,
    missing_message,
    not_active_message,
    session_name,
    summarize,
    viewed_refusal,
)
from orbit.protocol import (
    NOT_ACTIVE,
    PLANNER,
    PLANNER_LANE,
    UNKNOWN,
    WORK_LANE,
    ActivateSession,
    Attach,
    BusyLane,
    ChecksProposed,
    Command,
    DeleteSession,
    EventEnvelope,
    History,
    NewSession,
    RenameSession,
    ResumeStorage,
    ReviewStatus,
    RunningAgent,
    SessionRef,
    SessionSummary,
    Settings,
    Snapshot,
    StepState,
    TaskState,
)

VIEW_COMMANDS = (
    Attach,
    ActivateSession,
    NewSession,
    History,
    RenameSession,
    DeleteSession,
    ResumeStorage,
)
"""The commands a connection viewing a session that isn't open may send, ``ResumeStorage``
among them as a storage halt is the workspace's rather than the session's; the others are
refused as ``not_active``."""


class SessionParts(TypedDict):
    """The parts of a :class:`Snapshot` that are the session's own, rather than the
    workspace's."""

    session: SessionSummary
    settings: Settings
    busy: dict[BusyLane, bool]
    tasks: tuple[TaskState, ...]
    steps: tuple[StepState, ...]
    steps_title: NotRequired[str]
    review: ReviewStatus | None
    proposals: tuple[ChecksProposed, ...]
    agents: tuple[RunningAgent, ...]
    transcript: tuple[EventEnvelope, ...]
    active: NotRequired[bool]
    viewers: int


@dataclass(frozen=True)
class ViewParts:
    """What attaching uses: the ``keeper`` of the open session and the saved ones, the
    ``events`` it publishes into and reads the logged ones back from, the ``connections``, the
    sessions' ``states``, the ``settings``, the ``live`` snapshot of the open session, the
    storage ``halt`` and the ``takeover`` saying whether the orbit is read-only and the
    repository offered."""

    keeper: SessionKeeper
    events: Outbox
    connections: Connections
    states: SessionStates
    settings: SessionSettings
    live: Callable[[], Awaitable[Shown]]
    halt: StorageHalt
    takeover: Takeover


class SessionViews:
    """Attaches connections to the sessions in ``parts``: the open one, live, or a saved one as a
    read-only view, which runs nothing and publishes no events while it isn't open."""

    def __init__(self, parts: ViewParts) -> None:
        self._parts = parts
        self._deleted: set[str] = set()

    async def attach(self, connection: SessionConnection, session_id: str) -> None:
        """Have ``connection`` view the session ``session_id`` and publish its snapshot to it:
        the open session's as it stands, or a saved one's from its record and its last logged
        events. Refused (raising :class:`Refused`) for a session there's none of or that's
        unreadable; nothing changes if the saved sessions can't be listed, which is reported.
        If the saved one is deleted or opened while its events are read, :meth:`_show` says
        what's published instead."""
        parts = self._parts
        record = None if session_id == parts.keeper.id else await parts.keeper.saved(session_id)
        # It may have been deleted, or opened, while it was read.
        if session_id in self._deleted:
            raise Refused(UNKNOWN, missing_message(session_id))
        if session_id == parts.keeper.id:
            await self._attach_live(connection)
            return
        if record is None:
            return
        connection.attach(SessionRef(id=record.id, name=record.name))
        await self._show(record, [connection], attaching=True)
        parts.states.check()

    async def _attach_live(self, connection: SessionConnection) -> None:
        """Have ``connection`` view the open session, live, and publish its snapshot."""
        connection.attach(None)
        await (await self._parts.live()).publish(self._parts.events.put)

    def switching(self, kept: bool) -> None:
        """Have the connection whose command switches the open session view the session it
        switches to, and the others viewing the open session keep viewing it, if it's ``kept``
        saved, as a read-only view."""
        self._parts.connections.activate(self.open_session() if kept else None)

    async def activated(self, left: str) -> None:
        """Publish its read-only view to each connection, but the one asking, still attached
        to the session ``left``; the switch gave those viewing the one open in its place its
        live snapshot. Nothing if ``left`` is open again by then, or if it can't be read, which
        is reported."""
        parts = self._parts
        viewers = parts.connections.others_attached(left)
        record = await parts.keeper.saved(left) if viewers else None
        if record is not None:
            await self._show(record, viewers)

    async def _show(
        self, record: SessionRecord, viewers: list[SessionConnection], attaching: bool = False
    ) -> None:
        """Publish the read-only view of the session saved as ``record`` to each of the
        ``viewers`` still attached to it once it's read; nothing to one attached elsewhere
        meanwhile, which got that one's, or to one deleting it moved to the open session, which
        got the live snapshot. If it's open again by then, those still attached view it live:
        the switch published its snapshot to those listening, but not to viewers still
        ``attaching``, which this publishes it to."""
        view = await self._view(record)
        # One may have attached elsewhere while the view was read.
        still = attached_to(record.id, viewers)
        # Another switch may have opened it again meanwhile.
        if record.id == self._parts.keeper.id:
            for connection in still if attaching else []:
                await self._attach_live(connection)
            return
        for connection in still:
            await view.publish(connection.publish_here)

    def open_session(self) -> SessionRef:
        """The workspace's open session."""
        keeper = self._parts.keeper
        return SessionRef(id=keeper.id, name=keeper.name)

    def check_addressed(self, connection: SessionConnection, session: str | None) -> None:
        """Refuse a command from ``connection`` for ``session`` unless that's the session it
        views or none, which means that one."""
        viewing = None if connection.viewing is None else connection.viewing.id
        check_addressed_session(session, viewing, self._parts.keeper.id)

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

    async def deleted(self, session_id: str) -> None:
        """Have each connection still attached to the session ``session_id``, now deleted, view
        the open session, publishing its live snapshot to each of them alone, and publish that
        the deleted session has no viewers left."""
        parts = self._parts
        self._deleted.add(session_id)
        moved = parts.connections.attached(session_id)
        for connection in moved:
            connection.attach(None)
        if moved:
            live = await parts.live()
            # One may have attached elsewhere while the snapshot was taken.
            for connection in [c for c in moved if c.attached is None]:
                await live.publish(connection.publish_here)
        parts.states.check()

    def renamed(self, session_id: str, name: str) -> None:
        """Have each connection attached to the session ``session_id``, now renamed ``name``,
        name it so from now on."""
        named = session_name(name)
        assert named is not None  # An empty name is refused.
        self._parts.connections.renamed(SessionRef(id=session_id, name=named))

    def snapshot(self, **session: Unpack[SessionParts]) -> Snapshot:
        """The snapshot of a session made of its own parts, ``session``, and the workspace's as
        they stand: the storage halt, whether it's read-only and the repository offered, its
        open session and the model choices."""
        parts = self._parts
        return Snapshot(
            model_choices=parts.settings.model_choices,
            storage_halt=parts.halt.message,
            read_only=parts.takeover.read_only,
            ownership_offered=parts.takeover.offered,
            open_session=self.open_session(),
            **session,
        )

    async def _view(self, record: SessionRecord) -> Shown:
        """The read-only view of the session saved as ``record``: as it was saved, with its last
        logged events, nothing running in it, and why it shows none if they can't be read."""
        parts = self._parts
        transcript = await parts.events.transcript(record.id)
        view = self.snapshot(
            session=summarize(record),
            settings=record.role_settings.get(PLANNER, Settings()),
            busy={WORK_LANE: False, PLANNER_LANE: False},
            tasks=record.tasks,
            steps=(),
            review=None,
            proposals=(),
            agents=(),
            transcript=transcript.events,
            active=False,
            viewers=parts.states.of(record.id).viewers,
        )
        return Shown(view, transcript.error)


def attached_to(session_id: str, connections: list[SessionConnection]) -> list[SessionConnection]:
    """The ``connections`` attached to the session ``session_id``."""
    return [c for c in connections if c.attached_id == session_id]
