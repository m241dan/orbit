"""Tracing a frontend's connection to the control plane: the ``orbit.connection`` span from its
WebSocket being accepted until it's closed, with what the frontend said and did over it as
events (the remaining spans plan's decision 13).

The server starts and ends the span and records how the WebSocket closed; the control plane
records the frontend's ``Hello`` and its attaching and detaching on the span its connection
holds. The span is never current: the commands sent over the connection have spans of their own.
"""

from orbit.protocol import Hello
from orbit.tracing import AttributeValue, Span, SpanHandle, Telemetry, no_span

CONNECTION_SPAN = "orbit.connection"


def connection_span(telemetry: Telemetry, peer: tuple[str, int] | None) -> SpanHandle:
    """The span of a connection from ``peer``, its address and port, if known, starting its own
    trace."""
    attributes: dict[str, AttributeValue] = {}
    if peer is not None:
        attributes = {"network.peer.address": peer[0], "network.peer.port": peer[1]}
    with no_span():
        return telemetry.start(CONNECTION_SPAN, attributes)


def heard(span: Span, hello: Hello, last_seq: int) -> None:
    """Record on ``span`` the frontend's ``hello``, received once ``last_seq`` was stamped, and,
    if it gives a ``since``, that it's reconnecting, with how many were stamped after that."""
    span.set_attributes(
        {"orbit.client": hello.client, "orbit.protocol_version": hello.protocol_version}
    )
    span.event("hello")
    if hello.since is not None:
        span.event("reconnect", {"since": hello.since, "gap": last_seq - hello.since})


def attached(span: Span, workspace: str, session: str) -> None:
    """Record on ``span`` that its frontend attached to the session ``session`` of the workspace
    ``workspace``."""
    span.event("attach", {"orbit.workspace.id": workspace, "orbit.session.id": session})


def detached(span: Span) -> None:
    """Record on ``span`` that its frontend was detached from the workspace it was attached to."""
    span.event("detach")


def closed(span: Span, code: int, reason: str) -> None:
    """Record on ``span`` that its WebSocket closed with ``code`` and ``reason``."""
    span.event("close", {"code": code, "reason": reason})
