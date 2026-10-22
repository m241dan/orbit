"""The envelopes every command and event travels in.

An envelope's own fields come first, then ``payload``: the command or event, with its
``type``.
"""

from pydantic import Field

from orbit.protocol.commands import Command
from orbit.protocol.events import (
    AgentsChanged,
    Catalog,
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
)
"""The event types that belong to no session: their envelopes carry ``session=None``."""

CONNECTION_EVENTS: tuple[type[Event], ...] = (
    Welcome,
    Catalog,
    Snapshot,
    HistoryPage,
    AgentsChanged,
    UsageTotals,
)
"""The event types that answer one connection, and so are never kept in a session's log."""

CONTROL_PLANE_EVENTS: tuple[type[Event], ...] = (
    Welcome,
    Catalog,
    UsageChanged,
    WorkspaceCloning,
    AgentsChanged,
    UsageTotals,
)
"""The event types that belong to no workspace: their envelopes carry ``workspace=None``."""


class _Envelope(WireTolerant):
    """What commands' and events' envelopes share: the id of the ``workspace`` they belong to,
    none for the control plane's own, and the trace context, which isn't checked and isn't set
    yet."""

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


# The Snapshot's transcript and history pages hold envelopes, so they're complete once those
# are defined.
Snapshot.model_rebuild()
HistoryPage.model_rebuild()
EventEnvelope.model_rebuild()
