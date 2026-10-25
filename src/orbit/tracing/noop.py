"""Telemetry that records nothing, and gauges that report nothing, so traced code runs the same
with tracing off."""

from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager

from orbit.tracing.context import NoSpan, span_block
from orbit.tracing.port import Attributes, Carrier, Reading, Span


class NoTelemetry:
    """Telemetry whose spans ignore everything but still nest as the current span, and which
    carries no trace context."""

    def span(
        self, name: str, attributes: Attributes | None = None, links: Sequence[Span] = ()
    ) -> AbstractContextManager[NoSpan]:
        """A no-op span called ``name``, current for the block."""
        return span_block(self.start(name, attributes, links))

    def start(
        self, name: str, attributes: Attributes | None = None, links: Sequence[Span] = ()
    ) -> NoSpan:
        """A no-op span called ``name``, ignoring ``links``."""
        return NoSpan(name)

    def carry[E: Carrier](self, envelope: E) -> E:
        """``envelope`` unchanged: there's no trace context to carry."""
        return envelope

    def carried(self, envelope: Carrier) -> NoSpan:
        """A no-op span, whatever trace context ``envelope`` carries."""
        return NoSpan("carried")


class NoGauges:
    """Gauges that report nothing, never reading what's registered."""

    def register(
        self, name: str, unit: str, description: str, read: Callable[[], Iterable[Reading]]
    ) -> None:
        """Ignore the gauge ``name``."""
