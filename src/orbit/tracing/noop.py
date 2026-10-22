"""Telemetry that records nothing, so traced code runs the same with tracing off."""

from contextlib import AbstractContextManager

from orbit.tracing.context import NoSpan, span_block
from orbit.tracing.port import Attributes


class NoTelemetry:
    """Telemetry whose spans ignore everything but still nest as the current span."""

    def span(
        self, name: str, attributes: Attributes | None = None
    ) -> AbstractContextManager[NoSpan]:
        """A no-op span called ``name``, current for the block."""
        return span_block(self.start(name, attributes))

    def start(self, name: str, attributes: Attributes | None = None) -> NoSpan:
        """A no-op span called ``name``."""
        return NoSpan(name)
