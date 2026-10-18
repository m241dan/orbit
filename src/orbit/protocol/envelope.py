"""The envelopes every command and event travels in.

An envelope's own fields come first, then ``payload``: the command or event, with its
``type``.
"""

from pydantic import BaseModel, ConfigDict, Field

from orbit.protocol.messages import Catalog, Command, Event, SessionsListed, UsageChanged

DEFAULT_WORKSPACE = "default"
"""The one workspace's id, until there are workspaces."""

SESSIONLESS_EVENTS: tuple[type[Event], ...] = (UsageChanged, SessionsListed, Catalog)
"""The event types that belong to no session: their envelopes carry ``session=None``."""


class _Envelope(BaseModel):
    """What commands' and events' envelopes share: the ``workspace`` they belong to, and the
    trace context, which isn't checked and isn't set yet."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace: str = DEFAULT_WORKSPACE
    traceparent: str | None = None
    tracestate: str | None = None


class CommandEnvelope(_Envelope):
    """A command, ``payload``, with the ``command_id`` its sender chose for it, and the
    ``session`` it's for: ``None`` means the open session."""

    command_id: str = Field(min_length=1)
    session: str | None = None
    payload: Command


class EventEnvelope(_Envelope):
    """An event, ``payload``, with its place ``seq`` in the backend's stream, counted from 1,
    and the ``session`` it belongs to, if any."""

    seq: int = Field(ge=1)
    session: str | None = None
    payload: Event
