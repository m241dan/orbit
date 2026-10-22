"""The tracing port: spans orbit's packages record what they do on, whatever exports them.

It imports nothing of orbit's and only the standard library; ``NoTelemetry`` records nothing.
"""

from orbit.tracing.context import NoSpan, current_span, span_block, use_span
from orbit.tracing.noop import NoTelemetry
from orbit.tracing.port import Attributes, AttributeValue, Span, SpanHandle, Telemetry

__all__ = [
    "AttributeValue",
    "Attributes",
    "NoSpan",
    "NoTelemetry",
    "Span",
    "SpanHandle",
    "Telemetry",
    "current_span",
    "span_block",
    "use_span",
]
