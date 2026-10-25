"""The envelopes every command and event travels in.

An envelope's own fields come first, then ``payload``: the command or event, with its
``type``.
"""

from collections.abc import Mapping
from typing import Protocol, Self

from pydantic import Field

from orbit.protocol.commands import Command
from orbit.protocol.events import (
    AgentsChanged,
    Catalog,
    DirectoryListing,
    Event,
    HistoryPage,
    SessionStateChanged,
    Snapshot,
    UsageChanged,
    UsageTotals,
    Welcome,
    WorkspaceCloning,
)
from orbit.protocol.models import WireTolerant

SESSIONLESS_EVENTS: tuple[type[Event], ...] = (
    UsageChanged,
    Catalog,
    SessionStateChanged,
    WorkspaceCloning,
    AgentsChanged,
    UsageTotals,
    DirectoryListing,
)
"""The event types that belong to no session: their envelopes carry ``session=None``."""

CONNECTION_EVENTS: tuple[type[Event], ...] = (
    Welcome,
    Catalog,
    Snapshot,
    HistoryPage,
    AgentsChanged,
    UsageTotals,
    DirectoryListing,
)
"""The event types that answer one connection, and so are never kept in a session's log."""

CONTROL_PLANE_EVENTS: tuple[type[Event], ...] = (
    Welcome,
    Catalog,
    UsageChanged,
    WorkspaceCloning,
    AgentsChanged,
    UsageTotals,
    DirectoryListing,
)
"""The event types that belong to no workspace: their envelopes carry ``workspace=None``."""


class _Envelope(WireTolerant):
    """What commands' and events' envelopes share: the id of the ``workspace`` they belong to,
    none for the control plane's own, and the trace context, which isn't checked."""

    workspace: str | None = None
    traceparent: str | None = None
    tracestate: str | None = None


class CommandEnvelope(_Envelope):
    """A command, ``payload``, with the ``command_id`` its sender chose for it, and the
    ``workspace`` and ``session`` it's for: ``None`` means the one the connection is attached
    to and the session it views."""

    command_id: str = Field(min_length=1)
    session: str | None = None
    payload: Command


class EventEnvelope(_Envelope):
    """An event, ``payload``, with its place ``seq`` in the backend's stream, counted from 1,
    and the ``session`` it belongs to, if any."""

    seq: int = Field(ge=1)
    session: str | None = None
    payload: Event


class _Traced(Protocol):
    """What carries a trace context, as both envelopes do: its W3C ``traceparent`` and
    ``tracestate``, and copies carrying others. Telemetry passes the tracing port's carriers,
    which share this shape."""

    @property
    def traceparent(self) -> str | None:
        """The W3C ``traceparent`` carried, if any."""

    @property
    def tracestate(self) -> str | None:
        """The W3C ``tracestate`` carried, if any."""

    def __replace__(self, *, traceparent: str | None = ..., tracestate: str | None = ...) -> Self:
        """A copy carrying ``traceparent`` and ``tracestate`` instead."""


def trace_carrier(envelope: _Traced) -> dict[str, str]:
    """The trace context ``envelope`` carries, by W3C header name, holding only the fields
    set."""
    fields = {"traceparent": envelope.traceparent, "tracestate": envelope.tracestate}
    return {name: value for name, value in fields.items() if value is not None}


def with_trace_carrier[E: _Traced](envelope: E, carrier: Mapping[str, str]) -> E:
    """A copy of ``envelope`` carrying the trace context in ``carrier``, ignoring its other
    keys."""
    # copy.replace would type the copy as a _Traced rather than an E.
    traceparent, tracestate = carrier.get("traceparent"), carrier.get("tracestate")
    return envelope.__replace__(traceparent=traceparent, tracestate=tracestate)


# The Snapshot's transcript and history pages hold envelopes, so they're complete once those
# are defined.
Snapshot.model_rebuild()
HistoryPage.model_rebuild()
EventEnvelope.model_rebuild()
