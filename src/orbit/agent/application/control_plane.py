"""The control plane the frontends connect to, each connection an in-process implementation of
:class:`orbit.protocol.Session`, over the workspace hosts it routes their commands to."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import PurePath
from typing import Protocol

from orbit.agent.application.clones import Clones
from orbit.agent.application.closing import close_all
from orbit.agent.application.connection import (
    ConnectionParts,
    Connections,
    Place,
    Publish,
    SessionConnection,
)
from orbit.agent.application.handshake import HandshakeParts
from orbit.agent.application.outbox import Seqs
from orbit.agent.application.ports import Cloner, SeqMark, UsageSource
from orbit.agent.application.seq_mark import MarkReservations
from orbit.agent.application.usage_monitor import UsageMonitor
from orbit.agent.application.workspace_host import WorkspaceHost
from orbit.agent.domain.catalog import catalog
from orbit.agent.domain.clones import added_url
from orbit.agent.domain.refusals import STOPPING, Refused
from orbit.agent.domain.workspaces import (
    LOCAL_ONLY,
    PathKind,
    addressed_workspace,
    check_addable,
    check_directory,
    check_removable,
    served_workspace,
)
from orbit.protocol import (
    INTERNAL,
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
    RemoveWorkspace,
    TurnFinished,
    UsageChanged,
    WorkspaceEntry,
)


class WorkspaceUnavailable(Exception):
    """A workspace can't be served, for the reason ``message`` gives, refused with ``code``:
    ``INVALID`` when it's the workspace as given, such as an invalid configuration, or
    ``INTERNAL`` when orbit failed, such as the store, perhaps partway through opening it."""

    def __init__(self, message: str, code: str = INVALID) -> None:
        super().__init__(message)
        self.code = code


class WorkspaceSource(Cloner, Protocol):
    """Where the workspaces a control plane serves come from as frontends add them, by their
    paths or as clones of repositories it makes, and where they go as they're removed."""

    async def kind(self, path: str) -> PathKind:
        """What there is at ``path``."""
        ...

    async def top_level(self, path: str) -> str:
        """The path of the workspace the directory at ``path`` stands for: its repository's top
        level, if it's in one, else itself, resolved."""
        ...

    async def open(self, root: str, *, managed: bool = False) -> WorkspaceHost:
        """The host of the workspace at ``root``, registered or found by its path, flagged
        served and, if ``managed``, as a clone orbit made, with its lock taken if no other orbit
        holds it, making it read-only otherwise. Raises :class:`WorkspaceUnavailable` if it
        can't be served."""
        ...

    async def set_served(self, workspace_id: str, served: bool) -> None:
        """Flag the workspace ``workspace_id`` as ``served``, or not, so a server starting later
        serves it, or doesn't. Raises :class:`WorkspaceUnavailable` if that fails."""
        ...

    async def close(self, workspace_id: str) -> None:
        """Let go of the lock on the workspace ``workspace_id``, whose host is closed and which
        is no longer flagged served, keeping its records."""
        ...


def _last_seq(hosts: Iterable[WorkspaceHost], mark: SeqMark | None) -> int:
    """The highest ``seq`` any of ``hosts`` stamped, or ``mark``, if given, counts."""
    marked = 0 if mark is None else mark.max_seq()
    return max([marked, *(host.outbox.last_seq for host in hosts)])


async def _untold(entry: WorkspaceEntry) -> None:
    """Tell no one of ``entry``."""


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
    and remove one, closing its host, each followed by the ``Catalog``; one at a time, as each
    changes what's served across its awaits. An ``AddWorkspace`` with a URL is accepted at once,
    the repository cloned in the background by the source, with its progress and any failure
    told to every connection, and the clone then added as a path is, as a managed one. Without
    a source, as for a local orbit serving its own repository alone, they're refused as
    ``invalid``.

    Given the ``mark`` of the store every workspace's log shares, as ``orbit serve`` has, the
    ``seq`` count on past it, and each block of them is reserved there, once, as it's crossed,
    whether any workspace is served or not. Without one, each is reserved in the log of every
    workspace served, and those reserved while none was in that of the next one served.
    """

    def __init__(
        self,
        hosts: Iterable[WorkspaceHost] = (),
        usage: UsageSource | None = None,
        workspaces: WorkspaceSource | None = None,
        mark: SeqMark | None = None,
    ) -> None:
        self._hosts = {host.workspace_id: host for host in hosts}
        self._workspaces = workspaces
        self._changing = asyncio.Lock()
        self._reservations = None if mark is None else MarkReservations(mark)
        self._seqs = Seqs(_last_seq(self._hosts.values(), mark), self._reserve)
        self._lobby = Connections(lambda: None, lambda: "")  # Those attached to no workspace.
        self._events = _ControlEvents(self._seqs, self._everywhere)
        self._place = Place(self._events, self._events.stamp, self._lobby)
        self._usage = UsageMonitor(usage, self._events.put) if usage else None
        self._clones = Clones(self._events.announce)
        for host in self._hosts.values():
            self._serve(host)
        self._started = False
        self._closing = False

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

    def _reserve(self, seq: int) -> None:
        """Have every ``seq`` up to ``seq`` reserved: in the mark, if there is one, else in the
        log of every workspace served."""
        if self._reservations is not None:
            self._reservations.reserve(seq)
            return
        for host in self._hosts.values():
            host.outbox.reserve(seq)

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
            await self._route(envelope, connection)
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

    async def _route(self, envelope: CommandEnvelope, connection: SessionConnection) -> None:
        """Handle the command in ``envelope``, from ``connection`` once it's welcomed: the
        control plane's own here, the others in the workspace they're for."""
        command = envelope.payload
        assert not isinstance(command, Hello)  # The handshake answers it.
        if isinstance(command, AddWorkspace | RemoveWorkspace):
            await self._change(command, connection)
        elif isinstance(command, Attach):
            await self._attach(command, envelope.command_id, connection)
        else:
            host = self._addressed(envelope.workspace, envelope.session, connection)
            await host.handle(command, envelope.command_id, connection)

    async def _change(
        self, command: AddWorkspace | RemoveWorkspace, connection: SessionConnection
    ) -> None:
        """Add or remove the workspace ``command`` names, as ``connection`` asks, once no other
        change is under way, or start cloning the repository it names; refused without a
        source of workspaces, or once closing."""
        workspaces = self._workspaces
        if workspaces is None:
            raise Refused(INVALID, LOCAL_ONLY)
        if isinstance(command, RemoveWorkspace):
            async with self._locked():
                await self._remove(command.workspace, connection, workspaces)
            return
        url = added_url(command.path, command.url)
        if url is not None:
            self._clone(url, workspaces)
            return
        assert command.path is not None  # added_url refuses an AddWorkspace with neither.
        async with self._locked():
            await self._add(command.path, connection, workspaces)

    @asynccontextmanager
    async def _locked(self) -> AsyncIterator[None]:
        """Run the block once no other change is under way; refused once closing."""
        async with self._changing:
            self._check_open()
            yield

    def _check_open(self) -> None:
        """Refuse a change as ``invalid`` once closing."""
        if self._closing:
            raise Refused(INVALID, STOPPING)

    def _clone(self, url: str, workspaces: WorkspaceSource) -> None:
        """Have ``workspaces`` clone the repository at ``url`` in the background, then serve
        the clone, managed; refused once closing, or as ``busy`` while ``url`` is being cloned
        already."""
        self._check_open()

        async def serve(path: str) -> None:
            async with self._locked():
                await self._add(path, None, workspaces, managed=True)

        self._clones.start(url, workspaces, serve)

    async def _add(
        self,
        path: str,
        connection: SessionConnection | None,
        workspaces: WorkspaceSource,
        managed: bool = False,
    ) -> None:
        """Serve the workspace at ``path``, from ``workspaces``, as a clone orbit made if
        ``managed``, unless it's served already: start its host, opening its most recently
        active session and its background work, and send the catalog to ``connection``, if one
        asked, and every listening connection, or let it go again if that fails or is
        cancelled; refused unless ``path`` is the absolute path to a directory, or with the
        code it gives if it can't be served."""
        check_addable(path, absolute=PurePath(path).is_absolute())
        check_directory(path, await workspaces.kind(path))
        root = await workspaces.top_level(path)
        if any(host.path == root for host in self._hosts.values()):
            return
        try:
            host = await workspaces.open(root, managed=managed)
        except WorkspaceUnavailable as exc:
            raise Refused(exc.code, f"Can't add {root}: {exc}") from None
        try:
            await self._start(host, connection)
        except BaseException:
            try:
                await workspaces.set_served(host.workspace_id, False)
            finally:
                await self._let_go(host, workspaces)
            raise

    async def _start(self, host: WorkspaceHost, connection: SessionConnection | None) -> None:
        """Serve ``host``, as :meth:`_begin` does, and send the catalog to ``connection``, if
        given, and every listening connection. Its events are stamped and delivered as it opens
        its most recently active session, but no ``Attach`` reaches it till it's open."""
        self._serve(host)
        await self._begin(host)
        await self._changed(connection)

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

    async def _remove(
        self, workspace: str, connection: SessionConnection, workspaces: WorkspaceSource
    ) -> None:
        """Stop serving the workspace ``workspace``: have ``workspaces`` stop flagging it served,
        then close its host, having ``connection``, if it's attached there, attached to none,
        have ``workspaces`` let it go, and send the catalog to ``connection`` and every
        listening connection, even if closing the host fails; refused for a workspace not
        served, as ``busy`` while it runs work or another connection views one of its sessions,
        or with the code ``workspaces`` gives, still served, if its flag can't be cleared."""
        host = self._hosts[served_workspace(workspace, self._served)]
        self._check_removable(host)
        await self._unflag(host, workspaces)
        if connection.workspace == workspace:
            self._detach(connection)
        try:
            await self._let_go(host, workspaces)
        finally:
            await self._changed(connection)

    @staticmethod
    def _check_removable(host: WorkspaceHost) -> None:
        """Refuse removing ``host``'s workspace as ``busy`` while it runs work or another
        connection views one of its sessions."""
        viewed = host.connections.others_view()
        check_removable(host.path or host.workspace_id, host.activity, viewed)

    async def _unflag(self, host: WorkspaceHost, workspaces: WorkspaceSource) -> None:
        """Have ``workspaces`` stop flagging ``host``'s workspace served, flagging it again and
        refusing as :meth:`_check_removable` does if it got busy meanwhile; refused with the code
        ``workspaces`` gives if that fails."""
        name = host.path or host.workspace_id
        try:
            await workspaces.set_served(host.workspace_id, False)
        except WorkspaceUnavailable as exc:
            raise Refused(exc.code, f"Can't remove {name}, which is still served: {exc}") from None
        try:
            self._check_removable(host)
        except Refused:
            await workspaces.set_served(host.workspace_id, True)
            raise

    async def _let_go(self, host: WorkspaceHost, workspaces: WorkspaceSource) -> None:
        """Stop serving ``host``'s workspace: close ``host``, then have ``workspaces`` let it
        go, even if closing ``host`` fails."""
        self._hosts.pop(host.workspace_id, None)
        try:
            await host.close()
        finally:
            await workspaces.close(host.workspace_id)

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
        leave the workspace it's attached to, if any; refused for a workspace not served."""
        workspace = served_workspace(attach.workspace, self._served)
        left = connection.workspace
        if left is not None and left != workspace:
            self._detach(connection)
            self._hosts[left].check_state()  # Its sessions' viewers count one fewer.
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
        """Stop the clones under way, each removing what it cloned, then the usage monitor, if
        there is one, then close every host, which ends the streams of the connections attached
        to its workspace, then end the others', then reserve in the mark, if there is one, the
        ``seq`` still to be reserved; each is closed even if stopping one before it fails or the
        close is cancelled, as :func:`close_all` does."""
        self._closing = True
        await self._clones.close()  # A clone being served waits for the lock this takes.
        async with self._changing:  # A workspace being added is closed with the rest.
            await close_all(self._stops())

    def _stops(self) -> list[Callable[[], Awaitable[None]]]:
        """What :meth:`close` awaits, in order."""
        stops: list[Callable[[], Awaitable[None]]] = (
            [] if self._usage is None else [self._usage.close]
        )
        stops += [host.close for host in self._hosts.values()]
        stops.append(self._end_lobby)
        if self._reservations is not None:
            stops.append(self._reservations.close)
        return stops

    async def _end_lobby(self) -> None:
        """End the streams of the connections attached to no workspace."""
        self._lobby.end()
