"""The control-plane stream: the agents running across the workspaces served and what turns have
cost, told to the connections watching it."""

from collections.abc import Callable, Iterable, Sequence
from typing import Protocol

from orbit.agent.application.connection import Connections, SessionConnection
from orbit.agent.application.keeper import Clock
from orbit.agent.application.lanes import Background
from orbit.agent.application.ports import ReadFailed, UsageLedger
from orbit.agent.domain.streams import check_streams
from orbit.agent.domain.usage_totals import day_start, totals_event, usage_totals
from orbit.protocol import (
    CONTROL_PLANE_STREAM,
    INTERNAL,
    AgentExited,
    AgentRef,
    AgentsChanged,
    AgentStarted,
    ErrorOccurred,
    Event,
    EventEnvelope,
    UsageTotals,
)


class ControlEvents(Protocol):
    """The control plane's own events: stamped as no workspace's and no session's, or
    published to every listening connection."""

    def stamp(self, item: Event) -> EventEnvelope: ...

    async def put(self, item: Event) -> None: ...


class ControlStream:
    """The control-plane stream, its events stamped as the control plane's own ``events`` and
    told to the connections watching it among those ``everywhere`` gives: every agent running,
    by the starts and exits the workspaces' events tell, and the usage totals the ``ledger`` adds
    up, if there is one, today's from midnight as ``clock`` tells it, else none. A connection
    starting to watch is told both at once; the others, the agents whenever one starts or
    exits, and the totals, read afresh in the background, whenever they change: after each
    turn's row is written and after a workspace is purged. A failure escaping that is published
    in ``events``."""

    def __init__(
        self,
        events: ControlEvents,
        everywhere: Callable[[], Iterable[Connections]],
        ledger: UsageLedger | None,
        clock: Clock,
    ) -> None:
        self._stamp = events.stamp
        self._everywhere = everywhere
        self._ledger = ledger
        self._clock = clock
        self._agents: dict[tuple[str, str], AgentRef] = {}  # By workspace id and agent id.
        self._changes = 0  # How often the totals changed, so a read can tell it missed one.
        self._stale = False  # Whether the totals changed since they were last read.
        self._reader = Background("usage totals", events.put)

    async def watch(self, connection: SessionConnection, streams: Sequence[str]) -> None:
        """Have ``connection`` watch the ``streams`` from now on, in place of those it watched,
        telling it every agent running and the usage totals if it starts watching this one;
        refused as ``unknown``, changing nothing, unless each stream is one there is."""
        check_streams(streams)
        if CONTROL_PLANE_STREAM not in streams or CONTROL_PLANE_STREAM in connection.streams:
            connection.streams = frozenset(streams)
            return
        changes = self._changes
        totals = await self._totals()
        connection.streams = frozenset(streams)
        connection.tell(self._stamp(self._agents_changed()))
        connection.tell(self._stamp(totals))
        if self._changes != changes:  # A change during the read may be missing from it.
            self._refresh()

    def observe(self, workspace_id: str, envelope: EventEnvelope) -> None:
        """Note the agent ``envelope``, just published in the workspace ``workspace_id``, says
        started or exited, if it does, telling the watchers every agent running."""
        event = envelope.payload
        if isinstance(event, AgentStarted):
            assert envelope.session is not None, "An agent's events are its session's."
            self._agents[workspace_id, event.agent_id] = AgentRef(
                workspace=workspace_id,
                session=envelope.session,
                agent_id=event.agent_id,
                role=event.role,
                task_id=event.task_id,
            )
        elif (
            not isinstance(event, AgentExited)
            or self._agents.pop((workspace_id, event.agent_id), None) is None
        ):
            return
        self._broadcast(self._agents_changed())

    def totals_changed(self) -> None:
        """Note that the usage totals changed, as when a turn's row is written or a workspace's
        are deleted, telling the watchers, if any, the totals read afresh."""
        self._changes += 1
        if self._watchers():
            self._refresh()

    async def close(self) -> None:
        """Stop reading the usage totals."""
        await self._reader.close()

    def _refresh(self) -> None:
        """Read the usage totals afresh in the background and tell the watchers, once the read
        under way, if any, is told."""
        self._stale = True
        # A read under way reads again once it's told.
        self._reader.start(self._send_totals())

    async def _send_totals(self) -> None:
        """Tell the watchers the usage totals, read afresh until they didn't change meanwhile."""
        while self._stale:
            self._stale = False
            self._broadcast(await self._totals())

    async def _totals(self) -> UsageTotals | ErrorOccurred:
        """The usage totals now, or the error, coded ``internal``, saying they couldn't be
        read."""
        if self._ledger is None:
            return totals_event(usage_totals({}, {}, {}))
        try:
            totals = await self._ledger.totals(day_start(self._clock()))
        except ReadFailed as exc:
            return ErrorOccurred(code=INTERNAL, message=str(exc))
        return totals_event(totals)

    def _agents_changed(self) -> AgentsChanged:
        """Every agent running, oldest first."""
        return AgentsChanged(agents=tuple(self._agents.values()))

    def _watchers(self) -> list[SessionConnection]:
        """The connections watching the control-plane stream."""
        return [
            connection
            for connections in self._everywhere()
            for connection in connections.watching(CONTROL_PLANE_STREAM)
        ]

    def _broadcast(self, event: Event) -> None:
        """Stamp ``event`` and tell it to every watcher, if any."""
        watchers = self._watchers()
        if not watchers:
            return
        envelope = self._stamp(event)
        for connection in watchers:
            connection.tell(envelope)
