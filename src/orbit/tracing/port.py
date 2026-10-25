"""What tracing offers the code it traces: spans, the telemetry that starts them, and the
envelopes that carry their trace context between processes."""

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Protocol, Self

type AttributeValue = (
    str | bool | int | float | Sequence[str] | Sequence[bool] | Sequence[int] | Sequence[float]
)
"""What a span attribute may hold: a value, or a sequence of values of one kind."""

type Attributes = Mapping[str, AttributeValue]
"""A span's or an event's attributes, by name."""


class Span(Protocol):
    """One timed operation, with the attributes, events and failure recorded on it."""

    def set(self, **attributes: AttributeValue) -> None:
        """Set ``attributes`` on the span."""

    def set_attributes(self, attributes: Attributes) -> None:
        """Set ``attributes`` on the span, for names that aren't Python identifiers."""

    def event(self, name: str, attributes: Attributes | None = None) -> None:
        """Record that ``name`` happened now, with ``attributes``."""

    def error(self, message: str, exception: BaseException | None = None) -> None:
        """Mark the span failed with ``message``, recording ``exception`` as an event."""


class SpanHandle(Span, Protocol):
    """A span that outlives the block that starts it, and ends when told to."""

    def end(self) -> None:
        """End the span now."""

    def use(self) -> AbstractContextManager[Span]:
        """Make the span the current one for a block, without ending it."""


class Carrier(Protocol):
    """What carries a trace context between processes, as orbit's envelopes do: its W3C
    ``traceparent`` and ``tracestate``, and copies carrying others."""

    @property
    def traceparent(self) -> str | None:
        """The W3C ``traceparent`` carried, if any."""

    @property
    def tracestate(self) -> str | None:
        """The W3C ``tracestate`` carried, if any."""

    def __replace__(self, *, traceparent: str | None = ..., tracestate: str | None = ...) -> Self:
        """A copy carrying ``traceparent`` and ``tracestate`` instead."""


class Telemetry(Protocol):
    """What starts spans, each a child of the current span, if any, and carries the current
    span's trace context in envelopes."""

    def span(
        self, name: str, attributes: Attributes | None = None, links: Sequence[Span] = ()
    ) -> AbstractContextManager[Span]:
        """A span called ``name``, linked to each of ``links``, current for the block and ended
        on leaving it; an error escaping the block is recorded on it and re-raised."""

    def start(
        self, name: str, attributes: Attributes | None = None, links: Sequence[Span] = ()
    ) -> SpanHandle:
        """A span called ``name`` that runs until its ``end``, linked to each of ``links``: spans
        of this process, or remote ones from :meth:`carried`."""

    def carry[E: Carrier](self, envelope: E) -> E:
        """``envelope`` carrying the current span's trace context."""

    def carried(self, envelope: Carrier) -> Span:
        """The remote span whose trace context ``envelope`` carries, to make current so the
        spans started under it are its children, or to link to."""
