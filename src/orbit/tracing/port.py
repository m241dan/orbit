"""What tracing offers the code it traces: spans, and the telemetry that starts them."""

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Protocol

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


class Telemetry(Protocol):
    """What starts spans, each a child of the current span, if any."""

    def span(self, name: str, attributes: Attributes | None = None) -> AbstractContextManager[Span]:
        """A span called ``name``, current for the block and ended on leaving it; an error
        escaping the block is recorded on it and re-raised."""

    def start(self, name: str, attributes: Attributes | None = None) -> SpanHandle:
        """A span called ``name`` that runs until its ``end``."""
