"""The control plane the frontends connect to, each connection an in-process implementation of
:class:`orbit.protocol.Session`, over the workspace host it routes their commands to."""

from dataclasses import replace

from orbit.agent.application.connection import ConnectionParts, Publish, SessionConnection
from orbit.agent.application.handshake import HandshakeParts
from orbit.agent.application.ports import UsageSource
from orbit.agent.application.usage_monitor import UsageMonitor
from orbit.agent.application.workspace_host import WorkspaceHost
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.workspaces import addressed_workspace, served_workspace
from orbit.protocol import (
    INTERNAL,
    Attach,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    Event,
    EventEnvelope,
    Hello,
    TurnFinished,
    UsageChanged,
)


class ControlPlane:
    """What the frontends connect to: holds the workspace ``host``, routes every command to it
    and passes its events to the connections, and owns what's shared: the connections, the
    ``Outbox`` stamping every event's ``seq``, the handshake and, given a ``usage`` source, the
    monitor fetching the subscription's usage limits.

    Every stream opens with ``Hello``: nothing is published to it, and every other command is
    refused, until it's answered with ``Welcome``. A ``Catalog`` of what exists follows it, and
    nothing more until the connection's first ``Attach`` names a workspace served and a session
    there: that's answered with a ``Snapshot`` of the session (or, for the open session and a
    ``Hello`` whose ``since`` it can catch up from, its events after it), the latest usage if
    any was fetched, and the host's one-off notices; these reach only the connection they
    answer. Every other command goes to the workspace attached, which its envelope may name;
    one before the connection is attached is refused as ``invalid``, and one naming another
    workspace as ``wrong_session``, or ``unknown`` if it isn't served. Once the first
    connection attaches, the host's background work starts, and the usage limits are fetched in
    the background from then on, each ``UsageChanged`` reaching every connection attached.
    """

    def __init__(self, host: WorkspaceHost, usage: UsageSource | None = None) -> None:
        self._host = host
        self._events = host.outbox
        self._connections = host.connections
        self._usage = UsageMonitor(usage, self._events.put) if usage else None
        self._events.deliver = self._deliver
        self._started = False
        self._closing = False

    @property
    def host(self) -> WorkspaceHost:
        """The workspace this control plane serves."""
        return self._host

    @property
    def read_only(self) -> bool:
        """Whether another orbit owns the workspace's repository, so the work asked is queued."""
        return self._host.read_only

    @property
    def storage_halted(self) -> bool:
        """Whether a write to the store failed, so the workspace is halted until resumed."""
        return self._host.storage_halted

    def connect(self) -> SessionConnection:
        """A new connection to this control plane, with its own handshake and stream."""
        parts = ConnectionParts(
            HandshakeParts(self._host.catalog, self._latest_usage, lambda: self._events.last_seq),
            self._events,
            self._events.stamp,
            self._connections,
            self._answer,
        )
        return SessionConnection(parts)

    @property
    def _served(self) -> tuple[str, ...]:
        """The ids of the workspaces served."""
        return (self._host.workspace_id,)

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
                host = self._addressed(envelope, connection)
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

    def _addressed(self, envelope: CommandEnvelope, connection: SessionConnection) -> WorkspaceHost:
        """The host of the workspace ``connection`` is attached to, which ``envelope`` is for;
        refused unless the workspace and session it names, if any, are those attached."""
        addressed_workspace(envelope.workspace, connection.workspace, self._served)
        self._host.check_addressed(connection, envelope.session)
        return self._host

    async def _attach(self, attach: Attach, command_id: str, connection: SessionConnection) -> None:
        """Have ``connection`` view the session ``attach`` names, sent as ``command_id``: in the
        workspace it's attached to, as the host does, or else greeting it there; refused for a
        workspace not served."""
        served_workspace(attach.workspace, self._served)
        host = self._host
        if connection.workspace == host.workspace_id:
            await host.handle(attach, command_id, connection)
            return
        connection.workspace = host.workspace_id
        greeting = replace(
            host.greeting,
            start=self._start_background,
            closing=lambda: self._closing or host.closing,
        )
        try:
            await connection.handshake.join(
                greeting, attach.session, lambda session: host.attach(connection, session)
            )
        except BaseException:
            if not connection.greeted:
                connection.workspace = None
            raise

    def _deliver(self, envelope: EventEnvelope) -> None:
        """Pass ``envelope``, just published, to the connections it's for, then have the host
        publish its open session's state if that changed."""
        self._nudge_usage(envelope.payload)
        self._connections.deliver(envelope)
        self._host.check_state()

    def _start_background(self) -> None:
        """Start the host's background work and the usage monitor, if there is one, unless
        they were started for an earlier connection."""
        self._host.start_background()
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
        """Stop the usage monitor, if there is one, then close the host, which ends every
        connection's stream; the host is closed even if stopping the monitor fails, whose
        failure is raised after."""
        self._closing = True
        try:
            if self._usage is not None:
                await self._usage.close()
        finally:
            await self._host.close()
