"""The control plane the frontends connect to, each connection an in-process implementation of
:class:`orbit.protocol.Session`, over the workspace hosts it routes their commands to."""

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import replace

from orbit.agent.application.browsing import Browsing
from orbit.agent.application.clones import Clones
from orbit.agent.application.closing import close_all
from orbit.agent.application.coder_slots import CoderSlots
from orbit.agent.application.command_trace import CommandScope, accepted, command_span, refused
from orbit.agent.application.connection import (
    ConnectionParts,
    Connections,
    Place,
    Publish,
    SessionConnection,
)
from orbit.agent.application.connection_trace import attached, detached, heard
from orbit.agent.application.control_stream import ControlStream
from orbit.agent.application.failures import command_failure
from orbit.agent.application.handshake import HandshakeParts
from orbit.agent.application.keeper import Clock, local_now
from orbit.agent.application.outbox import Seqs
from orbit.agent.application.plane_gauges import PlaneGauges
from orbit.agent.application.ports import SeqMark, UsageLedger, UsageSource
from orbit.agent.application.seq_mark import MarkReservations
from orbit.agent.application.served_set import ServedParts, ServedSet
from orbit.agent.application.usage_monitor import UsageMonitor
from orbit.agent.application.workspace_host import WorkspaceHost
from orbit.agent.application.workspace_source import WorkspaceSource, WorkspaceUnavailable
from orbit.agent.domain.catalog import catalog
from orbit.agent.domain.listings import LOCAL_BROWSING
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.roles import Role
from orbit.agent.domain.workspaces import LOCAL_ONLY, addressed_workspace, served_workspace
from orbit.protocol import (
    INVALID,
    AddWorkspace,
    Attach,
    Catalog,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    Event,
    EventEnvelope,
    Hello,
    ListDirectory,
    RemoveWorkspace,
    TurnFinished,
    UsageChanged,
    Watch,
    WorkspaceEntry,
)
from orbit.tracing import Gauges, NoGauges, NoTelemetry, Span, Telemetry

_NO_TELEMETRY = NoTelemetry()
_NO_GAUGES = NoGauges()


def _last_seq(hosts: Iterable[WorkspaceHost], mark: SeqMark | None) -> int:
    """The highest ``seq`` any of ``hosts`` stamped, or ``mark``, if given, counts."""
    marked = 0 if mark is None else mark.max_seq()
    return max([marked, *(host.outbox.last_seq for host in hosts)])


def _across[T](
    hosts: Iterable[WorkspaceHost], items: Callable[[WorkspaceHost], Iterable[T]]
) -> list[T]:
    """The ``items`` each of ``hosts`` has, in turn."""
    return [item for host in hosts for item in items(host)]


def _listed_roles(host: WorkspaceHost) -> list[tuple[str, Role, bool]]:
    """``host``'s enabled roles, each with its name and whether it's personal."""
    personal = host.personal
    return [(name, role, name in personal) for name, role in host.roles.items()]


async def _untold(entry: WorkspaceEntry) -> None:
    """Tell no one of ``entry``."""


class _ControlEvents:
    """The control plane's own events, each stamped with the next of ``seqs`` as no workspace's
    and no session's, and the trace context of the span current then, as ``telemetry`` carries
    it, and delivered to every listening connection among those ``everywhere`` gives,
    whichever workspace it's attached to."""

    def __init__(
        self,
        seqs: Seqs,
        everywhere: Callable[[], Iterable[Connections]],
        telemetry: Telemetry,
    ) -> None:
        self._seqs = seqs
        self._everywhere = everywhere
        self._telemetry = telemetry

    def stamp(
        self, item: Event, viewed: str | None = None, *, logged: bool = True
    ) -> EventEnvelope:
        """``item`` in its envelope, stamped now; it's never logged."""
        return self._telemetry.carry(EventEnvelope(seq=self._seqs.next(), payload=item))

    def put_nowait(self, item: Event) -> None:
        """Stamp ``item`` and deliver it to every listening connection."""
        envelope = self.stamp(item)
        for connections in self._everywhere():
            connections.broadcast(envelope)

    async def put(self, item: Event) -> None:
        """Stamp ``item`` and deliver it to every listening connection."""
        self.put_nowait(item)

    def announce(self, item: Event) -> None:
        """Stamp ``item`` and deliver it to every listening connection and every one welcomed
        but attached to no workspace, as a clone's news is for whoever may have asked for it."""
        envelope = self.stamp(item)
        for connections in self._everywhere():
            connections.announce(envelope)


class ControlPlane:
    """What the frontends connect to: holds a host for each workspace served, ``hosts``, routes
    every command to the one it's for and passes each host's events to the connections attached
    to its workspace, and owns what's shared: the connections, the ``seq`` every event of every
    workspace is stamped with, the handshake, the ``Catalog`` and, given a ``usage`` source, the
    monitor fetching the subscription's usage limits.

    Every stream opens with ``Hello``: nothing is published to it, and every other command is
    refused, until it's answered with ``Welcome``. A ``Catalog`` of every workspace served
    follows it, and nothing more until the connection's first ``Attach`` names a workspace
    served and a session there: that attaches the connection to the workspace and is answered
    with a ``Snapshot`` of the session (or, for the open session and a ``Hello`` whose ``since``
    it can catch up from, its events after it), the latest usage if any was fetched, and the
    host's one-off notices; these reach only the connection they answer. A later ``Attach``
    naming another workspace served moves the connection there, leaving the one it was attached
    to and greeting it anew, or leaving it attached to none if that's refused. Every other
    command goes to the workspace attached, which its envelope may name; one before the
    connection is attached is refused as ``invalid``, and one naming another workspace as
    ``wrong_session``, or ``unknown`` if it isn't served. A connection gets
    the events of its workspace's session it views and those of its workspace that belong to
    no session, never another workspace's, and the control plane's own: the ``Catalog`` sent
    whenever a workspace's sessions change, and the usage. :meth:`start` opens each host's most
    recently active session and starts its background work, with the usage limits fetched in
    the background from then on, each ``UsageChanged`` reaching every connection attached; one
    not started, as for a local orbit, starts them once the first connection attaches.

    Given the ``workspaces`` source, ``AddWorkspace`` and ``RemoveWorkspace``, from any
    connection welcomed, attached or not, add a workspace to those served, starting its host,
    and remove one, closing its host and, if asked, purging its records, each followed by the
    ``Catalog``; one at a time, as each
    changes what's served across its awaits. An ``AddWorkspace`` with a URL is accepted at once,
    the repository cloned in the background by the source, with its progress and any failure
    told to every connection, and the clone then added as a path is, as a managed one. Without
    a source, as for a local orbit serving its own repository alone, they're refused as
    ``invalid``.

    Given ``browsing``, ``ListDirectory``, from any connection welcomed, is answered to that
    connection alone with the listing of a directory on the server's host, each directory a
    workspace is served at marked with its id, starting, with no path, beside the workspace the
    source added last; with a browsable root, it and ``AddWorkspace``
    with a path are refused as ``invalid`` outside it. Without, as for a local orbit,
    ``ListDirectory`` is refused as ``invalid``.

    Tasks whose roles work in the worktree, such as coders' and configurators', share the
    control plane's coder slots across every workspace served: as many run at once as
    :meth:`cap_coders` allows, any number until it's called, and the others wait in line, first
    come, first served, each session's state saying where.

    A connection welcomed, attached or not, chooses the optional streams it receives with
    ``Watch``: the control-plane stream tells it every agent running across the workspaces
    served, as each starts or exits, and the usage totals the ``ledger`` adds up, if there is
    one, today's from midnight as ``clock`` tells it, after each turn's row is written and
    after a workspace is purged; both as it starts watching too.

    Given the ``mark`` of the store every workspace's log shares, as ``orbit serve`` has, the
    ``seq`` count on past it, and each block of them is reserved there, once, as it's crossed,
    whether any workspace is served or not. Without one, each is reserved in the log of every
    workspace served, and those reserved while none was in that of the next one served.

    Every command is handled in an ``orbit.command`` span of the ``telemetry`` given, recording
    nothing by default, as :func:`command_span` says, and the control plane's own events carry
    the trace context of the span current as they're stamped. A connection's first ``Hello``,
    each ``Attach`` accepted and its detaching from a workspace are recorded on its
    :attr:`~SessionConnection.trace`.

    It reports live gauges through the ``gauges`` given, reporting nothing by default, as
    :class:`PlaneGauges` says: the agents running by role and workspace, the tasks waiting for a
    coder's slot, the pending tasks of each workspace's open session, the frontends welcomed and
    the bytes each workspace's sessions' logs hold in the store.
    """

    def __init__(
        self,
        hosts: Iterable[WorkspaceHost] = (),
        usage: UsageSource | None = None,
        workspaces: WorkspaceSource | None = None,
        mark: SeqMark | None = None,
        ledger: UsageLedger | None = None,
        clock: Clock = local_now,
        browsing: Browsing | None = None,
        telemetry: Telemetry = _NO_TELEMETRY,
        gauges: Gauges = _NO_GAUGES,
    ) -> None:
        self._telemetry = telemetry
        self._gauges = gauges
        self._hosts = {host.workspace_id: host for host in hosts}
        self._workspaces = workspaces
        self._browsing = browsing
        self._changing = asyncio.Lock()
        self._reservations = None if mark is None else MarkReservations(mark)
        self._seqs = Seqs(_last_seq(self._hosts.values(), mark), self._reserve)
        self._lobby = Connections(lambda: None, lambda: "")  # Those attached to no workspace.
        self._events = _ControlEvents(self._seqs, self._everywhere, telemetry)
        self._place = Place(self._events, self._events.stamp, self._lobby)
        self._usage = UsageMonitor(usage, self._events.put) if usage else None
        self._clones = Clones(self._events.announce)
        self._coders = CoderSlots(changed=self._check_states)
        self._stream = ControlStream(self._events, self._everywhere, ledger, clock)
        PlaneGauges(
            lambda: self.hosts, lambda: self._stream.agents, self._coders, self._lobby
        ).register(gauges)
        for host in self._hosts.values():
            self._serve(host)
        self._started = False
        self._closing = False
        self._served_set = None if workspaces is None else ServedSet(self._served_parts(workspaces))

    def _served_parts(self, workspaces: WorkspaceSource) -> ServedParts:
        """What the served set adds workspaces from ``workspaces`` and removes them with."""
        return ServedParts(
            hosts=self._hosts,
            workspaces=workspaces,
            changing=self._changing,
            closing=lambda: self._closing,
            coders=self._coders,
            clones=self._clones,
            browsing=self._browsing,
            serve=self._serve,
            begin=self._begin,
            detach=self._detach,
            changed=self._changed,
            totals_changed=self._stream.totals_changed,
        )

    def _serve(self, host: WorkspaceHost) -> None:
        """Have ``host`` stamp its events with the ``seq`` shared, deliver them to the
        connections attached to its workspace, and tell this of changes to its sessions; without
        a mark, the ``seq`` reserved before, as when none was served, are reserved in its log
        too."""
        if self._reservations is None and self._seqs.reserved > host.outbox.last_seq:
            host.outbox.reserve(self._seqs.reserved)
        self._seqs.catch_up(host.outbox.last_seq)
        host.outbox.seqs = self._seqs
        host.outbox.deliver = lambda envelope: self._deliver(host, envelope)
        host.catalog_changed = self._publish_catalog
        host.latest_usage = self._latest_usage
        host.coder_slots = self._coders
        host.usage_recorded = self._stream.totals_changed

    def _reserve(self, seq: int) -> None:
        """Have every ``seq`` up to ``seq`` reserved: in the mark, if there is one, else in the
        log of every workspace served."""
        if self._reservations is not None:
            self._reservations.reserve(seq)
            return
        for host in self._hosts.values():
            host.outbox.reserve(seq)

    @property
    def telemetry(self) -> Telemetry:
        """What traces the commands handled here."""
        return self._telemetry

    @property
    def gauges(self) -> Gauges:
        """What the live gauges are registered with."""
        return self._gauges

    def cap_coders(self, limit: int) -> None:
        """Run at most ``limit`` coders at once across the workspaces served, or any number if
        it's 0."""
        self._coders.cap(limit)

    @property
    def coders_running(self) -> int:
        """How many coders' slots are taken across the workspaces served."""
        return self._coders.taken

    def _check_states(self) -> None:
        """Have every host publish its open session's state if that changed, as the line for
        coders did."""
        for host in self._hosts.values():
            host.check_state()

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
        given, and the others with their sessions as they are now, and their agents' enabled
        roles, those any workspace converses with marked so, and each personal as the first
        workspace with a role of its name has it, the triggers and the lanes, each listed if any
        workspace has it."""
        hosts = tuple(self._hosts.values())  # Workspaces may be added or removed meanwhile.
        entries = [await self._entry(host, changed) for host in hosts]
        roles = _across(hosts, _listed_roles)
        triggers = _across(hosts, lambda host: host.triggers)
        lanes = _across(hosts, lambda host: host.lanes)
        conversational = _across(hosts, lambda host: host.conversational)
        return catalog(entries, roles, triggers, lanes, conversational)

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
        outcome. It's all traced in the command's span, current meanwhile."""
        with command_span(self._telemetry, envelope, self._scope(envelope, connection)) as span:
            outcome = await self._outcome(envelope, connection, span)
            if outcome is not None:
                await publish(outcome)

    @staticmethod
    def _scope(envelope: CommandEnvelope, connection: SessionConnection) -> CommandScope:
        """Who sent the command in ``envelope``, from ``connection``, and what it's for: the
        client a ``Hello`` names, or else the one ``connection`` was welcomed as, and the
        workspace and session an ``Attach`` names, or else those ``envelope`` names, or else
        those ``connection`` is attached to and views."""
        command = envelope.payload
        client = command.client if isinstance(command, Hello) else connection.handshake.client
        if isinstance(command, Attach):
            return CommandScope(client=client, workspace=command.workspace, session=command.session)
        return CommandScope(
            client=client,
            workspace=envelope.workspace or connection.workspace,
            session=envelope.session or connection.viewed_id,
        )

    async def _outcome(
        self, envelope: CommandEnvelope, connection: SessionConnection, span: Span
    ) -> CommandAccepted | CommandRefused | None:
        """Handle the command in ``envelope``, from ``connection``, recording how it came out on
        its ``span``, and return its outcome to publish, as :meth:`_answer` says: none for a
        ``Hello`` welcomed, as the handshake published ``Welcome``."""
        command_id = envelope.command_id
        command = envelope.payload
        try:
            if isinstance(command, Hello):
                self._heard(command, connection)
                await connection.handshake.hello(command, command_id)
            else:
                connection.handshake.admit()
                await self._route(envelope, connection)
        except Exception as exc:  # A bug in one handler mustn't leave its command unanswered.
            code, message = command_failure(command, exc)
            refused(span, code, message, exc)
            return CommandRefused(command_id=command_id, code=code, message=message)
        accepted(span)
        return None if isinstance(command, Hello) else CommandAccepted(command_id=command_id)

    def _heard(self, hello: Hello, connection: SessionConnection) -> None:
        """Record ``hello`` on ``connection``'s trace, unless a ``Hello`` was welcomed there
        already."""
        if not connection.handshake.welcomed:
            heard(connection.trace, hello, self._seqs.last)

    async def _route(self, envelope: CommandEnvelope, connection: SessionConnection) -> None:
        """Handle the command in ``envelope``, from ``connection`` once it's welcomed: the
        control plane's own here, the others in the workspace they're for."""
        command = envelope.payload
        assert not isinstance(command, Hello)  # The handshake answers it.
        if isinstance(command, AddWorkspace | RemoveWorkspace):
            await self._change(command, connection)
        elif isinstance(command, Attach):
            await self._attach(command, envelope.command_id, connection)
        elif isinstance(command, Watch):
            await self._stream.watch(connection, command.streams)
        elif isinstance(command, ListDirectory):
            await self._list(command, connection)
        else:
            host = self._addressed(envelope.workspace, envelope.session, connection)
            await host.handle(command, envelope.command_id, connection)

    async def _change(
        self, command: AddWorkspace | RemoveWorkspace, connection: SessionConnection
    ) -> None:
        """Add or remove the workspace ``command`` names, as ``connection`` asks, as the served
        set does; refused without a source of workspaces."""
        if self._served_set is None:
            raise Refused(INVALID, LOCAL_ONLY)
        await self._served_set.change(command, connection)

    async def _list(self, command: ListDirectory, connection: SessionConnection) -> None:
        """Send ``connection`` alone the listing of the directory ``command`` names; refused
        without browsing, as for a local orbit, or as browsing refuses it."""
        if self._browsing is None:
            raise Refused(INVALID, LOCAL_BROWSING)
        served = {host.path: host.workspace_id for host in self._hosts.values() if host.path}
        latest = None if command.path is not None else await self._latest_added()
        listing = await self._browsing.listing(command.path, served, latest)
        await connection.publish_here(listing)

    async def _latest_added(self) -> str | None:
        """The path of the workspace served that was flagged served last, if any; refused when
        that can't be found out."""
        if self._workspaces is None:
            return None
        try:
            return await self._workspaces.latest_added()
        except WorkspaceUnavailable as exc:
            raise Refused(exc.code, f"Can't find where to start listing: {exc}") from None

    async def start(self) -> None:
        """Start serving the hosts this was built with, as :meth:`_begin` does, one by one, with
        no frontend connected; raises what opening a session raises, such as ``RuntimeError``
        when a store fails."""
        for host in self.hosts:
            await self._begin(host)

    async def _begin(self, host: WorkspaceHost) -> None:
        """Have ``host`` open its most recently active session, then serve it and start its
        background work."""
        await self._open_latest(host)
        self._hosts[host.workspace_id] = host
        self._start_background(host)

    async def _open_latest(self, host: WorkspaceHost) -> None:
        """Have ``host`` open its most recently active session, telling no one of the change to
        its sessions, as the catalog follows once it's started."""
        host.catalog_changed = _untold
        try:
            await host.open_latest()
        finally:
            host.catalog_changed = self._publish_catalog

    async def _changed(self, connection: SessionConnection | None) -> None:
        """Send every listening connection the catalog, as the workspaces served changed, and
        ``connection``, which changed them, even if it isn't listening yet; for a clone, which
        no connection waits on, every one welcomed but attached to none too."""
        catalog = await self._catalog()
        if connection is None:
            self._events.announce(catalog)
            return
        await self._events.put(catalog)
        if not connection.listening:
            await connection.publish_here(catalog)

    def _detach(self, connection: SessionConnection) -> None:
        """Have ``connection`` attached to no workspace again, as one that's welcomed; its next
        ``Attach`` greets it anew."""
        connection.move(self._place)
        connection.workspace = None
        connection.handshake.leave()
        detached(connection.trace)

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
        workspace it's attached to, as the host does, or else greeting it there, having it
        leave the workspace it's attached to, if any; refused for a workspace not served. Once
        it views the session, that's recorded on ``connection``'s trace."""
        workspace = served_workspace(attach.workspace, self._served)
        left = connection.workspace
        if left is not None and left != workspace:
            self._detach(connection)
            self._hosts[left].check_state()  # Its sessions' viewers count one fewer.
        if connection.workspace is None:
            await self._join(self._hosts[workspace], attach.session, connection)
        else:
            host = self._addressed(workspace, None, connection)
            await host.handle(attach, command_id, connection)
        attached(connection.trace, workspace, attach.session)

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
        workspace it's for, and the agents it starts or ends to the control-plane stream, then
        have ``host`` publish its open session's state if that changed."""
        self._nudge_usage(envelope.payload)
        host.connections.deliver(envelope)
        self._stream.observe(host.workspace_id, envelope)
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
        """Stop the clones under way, each removing what it cloned, then the usage monitor, if
        there is one, and the reading of the usage totals, then close every host, which ends the
        streams of the connections attached to its workspace, then end the others', then reserve
        in the mark, if there is one, the ``seq`` still to be reserved; each is closed even if
        stopping one before it fails or the close is cancelled, as :func:`close_all` does."""
        self._closing = True
        await self._clones.close()  # A clone being served waits for the lock this takes.
        async with self._changing:  # A workspace being added is closed with the rest.
            await close_all(self._stops())

    def _stops(self) -> list[Callable[[], Awaitable[None]]]:
        """What :meth:`close` awaits, in order."""
        stops: list[Callable[[], Awaitable[None]]] = (
            [] if self._usage is None else [self._usage.close]
        )
        stops.append(self._stream.close)
        stops += [host.close for host in self._hosts.values()]
        stops.append(self._end_lobby)
        if self._reservations is not None:
            stops.append(self._reservations.close)
        return stops

    async def _end_lobby(self) -> None:
        """End the streams of the connections attached to no workspace."""
        self._lobby.end()
