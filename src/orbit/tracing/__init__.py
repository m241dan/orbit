"""The tracing port: spans orbit's packages record what they do on, and gauges they report live
values through, whatever exports them.

It imports nothing of orbit's and only the standard library; ``NoTelemetry`` records nothing, and
``NoGauges`` reports nothing.
"""

from orbit.tracing.context import NoSpan, current_span, no_span, span_block, use_span
from orbit.tracing.noop import NoGauges, NoTelemetry
from orbit.tracing.port import (
    Attributes,
    AttributeValue,
    Carrier,
    Gauges,
    Reading,
    Span,
    SpanHandle,
    Telemetry,
)

__all__ = [
    "AttributeValue",
    "Attributes",
    "Carrier",
    "Gauges",
    "NoGauges",
    "NoSpan",
    "NoTelemetry",
    "Reading",
    "Span",
    "SpanHandle",
    "Telemetry",
    "current_span",
    "no_span",
    "span_block",
    "use_span",
]
