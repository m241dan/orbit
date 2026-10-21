"""The control plane the frontends connect to, each connection an in-process implementation of
:class:`orbit.protocol.Session`, over the workspace hosts it routes their commands to."""

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import replace

from orbit.agent.application.connection import (
    ConnectionParts,
    Connections,
    Place,
    Publish,
    SessionConnection,
)
from orbit.agent.application.handshake import HandshakeParts
from orbit.agent.application.outbox import Seqs
from orbit.agent.application.ports import UsageSource
from orbit.agent.application.usage_monitor import UsageMonitor
from orbit.agent.application.workspace_host import WorkspaceHost
from orbit.agent.domain.catalog import catalog
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.workspaces import addressed_workspace, served_workspace
from orbit.protocol import (
    INTERNAL,
    Attach,
    Catalog,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    Event,
    EventEnvelope,
    Hello,
    TurnFinished,
    UsageChanged,
    WorkspaceEntry,
)


class _ControlEvents:
    """The control plane's own events, each stamped with the next of ``seqs`` as no workspace's
    and no session's, and delivered to every listening connection among those ``everywhere``
    gives, whichever workspace it's attached to."""

    def __init__(self, seqs: Seqs, everywhere: Callable[[], Iterable[Connections]]) -> None:
        self._seqs = seqs
        self._everywhere = everywhere

    def stamp(
        self, item: Event, viewed: str | None = None, *, logged: bool = True
    ) -> EventEnvelope:
        """``item`` in its envelope, stamped now; it's never logged."""
        return EventEnvelope(seq=self._seqs.next(), payload=item)

    def put_nowait(self, item: Event) -> None:
        """Stamp ``item`` and deliver it to every listening connection."""
        envelope = self.stamp(item)
        for connections in self._everywhere():
            connections.broadcast(envelope)

    async def put(self, item: Event) -> None:
        """Stamp ``item`` and deliver it to every listening connection."""
        self.put_nowait(item)


class ControlPlane:
    """What the frontends connect to: holds a host for each workspace served, ``hosts``, routes
    every command to the one it's for and passes each host's events to the connections attached
    to its workspace, and owns what's shared: the connections, the ``seq`` every event of every
    workspace is stamped with, the handshake, the ``Catalog`` and, given a ``usage`` source, the
    monitor fetching the subscription's usage limits.

    Every stream opens with ``Hello``: nothing is published to it, and every other command is
    refused, until it's answered with ``Welcome``. A ``Catalog`` of every workspace served
    follows it, and nothing more until the connection's first ``Attach`` names a workspace
    served and a session there: that attaches the connection to the workspace, for good, and is
    answered with a ``Snapshot`` of the session (or, for the open session and a ``Hello`` whose
    ``since`` it can catch up from, its events after it), the latest usage if any was fetched,
    and the host's one-off notices; these reach only the connection they answer. Every other
    command goes to the workspace attached, which its envelope may name; one before the
    connection is attached is refused as ``invalid``, and one naming another workspace, an
    ``Attach`` too, as ``wrong_session``, or ``unknown`` if it isn't served. A connection gets
    the events of its workspace's session it views and those of its workspace that belong to
    no session, never another workspace's, and the control plane's own: the ``Catalog`` sent
    whenever a workspace's sessions change, and the usage. Once the first connection attaches
    to a workspace, its host's background work starts, and the usage limits are fetched in the
    background from then on, each ``UsageChanged`` reaching every connection attached.
    """

    def __init__(
        self, hosts: Iterable[WorkspaceHost] = (), usage: UsageSource | None = None
    ) -> None:
        self._hosts = {host.workspace_id: host for host in hosts}
        self._seqs = Seqs(max((host.outbox.last_seq for host in self._hosts.values()), default=0))
        self._lobby = Connections(lambda: None, lambda: "")  # Those attached to no workspace.
        self._events = _ControlEvents(self._seqs, self._everywhere)
        self._place = Place(self._events, self._events.stamp, self._lobby)
        self._usage = UsageMonitor(usage, self._events.put) if usage else None
        for host in self._hosts.values():
            self._serve(host)
        self._started = False
        self._closing = False

    def _serve(self, host: WorkspaceHost) -> None:
        """Have ``host`` stamp its events with the ``seq`` shared, deliver them to the
        connections attached to its workspace, and tell this of changes to its sessions."""
        host.outbox.seqs = self._seqs
        host.outbox.deliver = lambda envelope: self._deliver(host, envelope)
        host.catalog_changed = self._publish_catalog
        host.latest_usage = self._latest_usage

    @property
    def hosts(self) -> tuple[WorkspaceHost, ...]:
        """The hosts of the workspaces served."""
        return tuple(self._hosts.values())

    @property
    def read_only(self) -> bool:
        """Whether another orbit owns the repository of any workspace served, so the work asked
        there is queued."""
        return any(host.read_only for host in self._hosts.values())

    @property
    def storage_halted(self) -> bool:
        """Whether a write to the store failed in any workspace served, so it's halted until
        resumed."""
        return any(host.storage_halted for host in self._hosts.values())

    def connect(self) -> SessionConnection:
        """A new connection to this control plane, with its own handshake and stream."""
        handshake = HandshakeParts(self._catalog, self._latest_usage, lambda: self._seqs.last)
        return SessionConnection(ConnectionParts(handshake, self._place, self._answer))

    @property
    def _served(self) -> tuple[str, ...]:
        """The ids of the workspaces served."""
        return tuple(self._hosts)

    def _everywhere(self) -> list[Connections]:
        """The connections attached to no workspace, then those attached to each."""
        return [self._lobby, *(host.connections for host in self._hosts.values())]

    async def _catalog(self, changed: WorkspaceEntry | None = None) -> Catalog:
        """What exists: every workspace served, the one ``changed`` names with that entry, if
        given, and the others with their sessions as they are now, and their agents' roles."""
        entries = [await self._entry(host, changed) for host in self._hosts.values()]
        roles = (role for host in self._hosts.values() for role in host.roles)
        return catalog(entries, roles)

    @staticmethod
    async def _entry(host: WorkspaceHost, changed: WorkspaceEntry | None) -> WorkspaceEntry:
        """``host``'s entry in the catalog: ``changed``, if it's of ``host``'s workspace."""
        if changed is not None and changed.id == host.workspace_id:
            return changed
        return await host.entry()

    async def _publish_catalog(self, changed: WorkspaceEntry) -> None:
        """Send every listening connection the catalog, with the workspace ``changed`` names as
        it is now."""
        await self._events.put(await self._catalog(changed))

    async def _answer(
        self, envelope: CommandEnvelope, connection: SessionConnection, publish: Publish
    ) -> None:
        """Handle the command in ``envelope``, from ``connection``, then ``publish`` its
        outcome: ``CommandRefused`` if it was refused, as it is when it's for no workspace
        attached or the host refuses it, or as ``internal`` if handling it failed, else
        ``CommandAccepted``, or ``Welcome`` for a ``Hello``. Only cancellation escapes, with no
        outcome."""
        command_id = envelope.command_id
        command = envelope.payload
        try:
            if isinstance(command, Hello):
                await connection.handshake.hello(command, command_id)
                return
            connection.handshake.admit()
            if isinstance(command, Attach):
                await self._attach(command, command_id, connection)
            else:
                host = self._addressed(envelope.workspace, envelope.session, connection)
                await host.handle(command, command_id, connection)
        except Refused as refusal:
            await publish(
                CommandRefused(command_id=command_id, code=refusal.code, message=str(refusal))
            )
            return
        except Exception as exc:  # A bug in one handler mustn't leave its command unanswered.
            message = f"Orbit failed to handle {type(envelope.payload).__name__}: {exc}"
            await publish(CommandRefused(command_id=command_id, code=INTERNAL, message=message))
            return
        await publish(CommandAccepted(command_id=command_id))

    def _addressed(
        self, workspace: str | None, session: str | None, connection: SessionConnection
    ) -> WorkspaceHost:
        """The host of the workspace ``connection`` is attached to, which a command for the
        ``workspace`` and ``session`` it names, if any, is for; refused unless they're those
        attached."""
        attached = addressed_workspace(workspace, connection.workspace, self._served)
        host = self._hosts[attached]
        host.check_addressed(connection, session)
        return host

    async def _attach(self, attach: Attach, command_id: str, connection: SessionConnection) -> None:
        """Have ``connection`` view the session ``attach`` names, sent as ``command_id``: in the
        workspace it's attached to, as the host does, or else greeting it there; refused for a
        workspace not served, or another than the one it's attached to."""
        workspace = served_workspace(attach.workspace, self._served)
        if connection.workspace is None:
            await self._join(self._hosts[workspace], attach.session, connection)
            return
        host = self._addressed(workspace, None, connection)
        await host.handle(attach, command_id, connection)

    async def _join(self, host: WorkspaceHost, session: str, connection: SessionConnection) -> None:
        """Attach ``connection`` to ``host``'s workspace, viewing the session ``session`` there,
        and greet it; it's attached to none again if that's refused."""
        connection.move(host.place)
        connection.workspace = host.workspace_id
        greeting = replace(
            host.greeting,
            start=lambda: self._start_background(host),
            closing=lambda: self._closing or host.closing,
        )
        try:
            await connection.handshake.join(
                greeting, session, lambda viewed: host.attach(connection, viewed)
            )
        except BaseException:
            if not connection.greeted:
                connection.workspace = None
                connection.move(self._place)
            raise

    def _deliver(self, host: WorkspaceHost, envelope: EventEnvelope) -> None:
        """Pass ``envelope``, just published by ``host``, to the connections attached to its
        workspace it's for, then have ``host`` publish its open session's state if that
        changed."""
        self._nudge_usage(envelope.payload)
        host.connections.deliver(envelope)
        host.check_state()

    def _start_background(self, host: WorkspaceHost) -> None:
        """Start ``host``'s background work and the usage monitor, if there is one, unless
        they were started for an earlier connection."""
        host.start_background()
        if self._started:
            return
        self._started = True
        if self._usage is not None:
            self._usage.start()

    def _nudge_usage(self, event: Event) -> None:
        """Have the usage monitor fetch again, if due, once ``event`` ends an agent's turn."""
        if self._usage is not None and isinstance(event, TurnFinished) and event.agent_id:
            self._usage.nudge()

    def _latest_usage(self) -> UsageChanged | None:
        """The usage limits fetched last; none without a usage monitor, or before a fetch."""
        return self._usage.latest if self._usage else None

    async def close(self) -> None:
        """Stop the usage monitor, if there is one, then close every host, which ends the
        streams of the connections attached to its workspace, then end the others'; each is
        closed even if stopping one before it fails, whose failure is raised after."""
        self._closing = True
        failure: Exception | None = None
        for stop in self._stops():
            try:
                await stop()
            except Exception as exc:  # A bug; the rest must still stop.
                failure = failure or exc
        if failure is not None:
            raise failure

    def _stops(self) -> list[Callable[[], Awaitable[None]]]:
        """What :meth:`close` awaits, in order."""
        stops: list[Callable[[], Awaitable[None]]] = (
            [] if self._usage is None else [self._usage.close]
        )
        stops += [host.close for host in self._hosts.values()]
        stops.append(self._end_lobby)
        return stops

    async def _end_lobby(self) -> None:
        """End the streams of the connections attached to no workspace."""
        self._lobby.end()
