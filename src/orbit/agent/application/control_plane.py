"""The control plane the frontends connect to, each connection an in-process implementation of
:class:`orbit.protocol.Session`, over the workspace host it routes their commands to."""

from orbit.agent.application.connection import ConnectionParts, Publish, SessionConnection
from orbit.agent.application.handshake import HandshakeParts
from orbit.agent.application.ports import UsageSource
from orbit.agent.application.usage_monitor import UsageMonitor
from orbit.agent.application.workspace_host import WorkspaceHost
from orbit.agent.domain.refusals import Refused
from orbit.protocol import (
    DEFAULT_WORKSPACE,
    INTERNAL,
    UNKNOWN,
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
    refused, until it's answered with ``Welcome``. A ``Catalog`` of what exists follows it, then
    the latest usage if any was fetched, a ``Snapshot`` of the session open (or, for a ``Hello``
    whose ``since`` it can catch up from, the open session's events after it) and the host's
    one-off notices; these reach only the connection whose ``Hello`` they answer. Once the
    first is welcomed, the host's background work starts, and the usage limits are fetched in
    the background from then on, each ``UsageChanged`` reaching every connection.
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
            self._handshake_parts(),
            self._events,
            self._events.stamp,
            self._connections,
            self._answer,
        )
        return SessionConnection(parts)

    def _handshake_parts(self) -> HandshakeParts:
        """What the handshake publishes after ``Welcome``, and starts once it has."""
        greeting = self._host.greeting
        return HandshakeParts(
            greeting.catalog,
            self._latest_usage,
            greeting.snapshot,
            greeting.missed,
            greeting.load_review_count,
            greeting.workspace,
            greeting.no_steps,
            greeting.startup_notes,
            greeting.leftovers,
            self._start_background,
            lambda: self._closing or self._host.closing,
        )

    async def _answer(
        self, envelope: CommandEnvelope, connection: SessionConnection, publish: Publish
    ) -> None:
        """Handle the command in ``envelope``, from ``connection``, then ``publish`` its
        outcome: ``CommandRefused`` if it was refused, as it is when it names no workspace
        served or the host refuses it, or as ``internal`` if handling it failed, else
        ``CommandAccepted``, or ``Welcome`` for a ``Hello``. Only cancellation escapes, with no
        outcome."""
        command_id = envelope.command_id
        handshake = connection.handshake
        try:
            host = self._host_for(envelope.workspace)
            host.check_addressed(connection, envelope.session)
            command = envelope.payload
            if isinstance(command, Hello):
                await handshake.hello(command, command_id)
                return
            handshake.admit()
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

    def _host_for(self, workspace: str) -> WorkspaceHost:
        """The host of ``workspace``; refused unless it's the one served."""
        if workspace != DEFAULT_WORKSPACE:
            message = f"No workspace {workspace!r}; the workspace is {DEFAULT_WORKSPACE}."
            raise Refused(UNKNOWN, message)
        return self._host

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
