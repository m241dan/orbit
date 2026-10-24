"""The tracing port over the OpenTelemetry SDK: spans batched to an exporter, the current span
following orbit's own, and the trace context carried in envelopes."""

import logging
import threading
from collections.abc import Sequence
from contextlib import AbstractContextManager

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from orbit.protocol import CommandEnvelope, EventEnvelope, trace_carrier, with_trace_carrier
from orbit.telemetry.run import FLUSH_SECONDS
from orbit.tracing import Attributes, AttributeValue, Span, current_span, span_block, use_span

logger = logging.getLogger(__name__)

_PROPAGATOR = TraceContextTextMapPropagator()


class OtelSpan:
    """A span of the port over the OpenTelemetry span ``otel``."""

    def __init__(self, otel: trace.Span) -> None:
        self.otel = otel

    def set(self, **attributes: AttributeValue) -> None:
        """Set ``attributes`` on the span."""
        self.otel.set_attributes(attributes)

    def set_attributes(self, attributes: Attributes) -> None:
        """Set ``attributes`` on the span."""
        self.otel.set_attributes(attributes)

    def event(self, name: str, attributes: Attributes | None = None) -> None:
        """Record that ``name`` happened now, with ``attributes``."""
        self.otel.add_event(name, attributes)

    def error(self, message: str, exception: BaseException | None = None) -> None:
        """Mark the span failed with ``message``, recording ``exception`` as an event."""
        self.otel.set_status(Status(StatusCode.ERROR, message))
        if exception is not None:
            self.otel.record_exception(exception)

    def end(self) -> None:
        """End the span now."""
        self.otel.end()

    def use(self) -> AbstractContextManager[Span]:
        """Make the span the current one for a block, without ending it."""
        return use_span(self)


type Envelope = CommandEnvelope | EventEnvelope


class OtelTelemetry:
    """Telemetry over ``provider``, whose spans nest under orbit's current span; ``counts``
    tells how many it couldn't export, and :meth:`close` waits at most ``flush_seconds`` for
    those not yet exported."""

    def __init__(
        self, provider: TracerProvider, counts: SpanCounts, flush_seconds: float = FLUSH_SECONDS
    ) -> None:
        self._provider = provider
        self._tracer = provider.get_tracer("orbit")
        self._counts = counts
        self._flush_seconds = flush_seconds

    def span(
        self, name: str, attributes: Attributes | None = None
    ) -> AbstractContextManager[OtelSpan]:
        """A span called ``name``, current for the block and ended on leaving it."""
        return span_block(self.start(name, attributes))

    def start(self, name: str, attributes: Attributes | None = None) -> OtelSpan:
        """A span called ``name``, a child of the current span, running until its ``end``."""
        otel = self._tracer.start_span(
            name, context=_context(current_span()), attributes=attributes
        )
        return OtelSpan(otel)

    def carry[E: Envelope](self, envelope: E) -> E:
        """``envelope`` carrying the current span's trace context."""
        carrier: dict[str, str] = {}
        _PROPAGATOR.inject(carrier, context=_context(current_span()))
        return with_trace_carrier(envelope, carrier)

    def carried(self, envelope: Envelope) -> OtelSpan:
        """The remote span whose trace context ``envelope`` carries, to make current so the
        spans started under it are its children; one recording nothing if it carries none."""
        context = _PROPAGATOR.extract(trace_carrier(envelope))
        return OtelSpan(trace.get_current_span(context))

    def close(self) -> None:
        """Export the spans not yet exported, waiting at most the flush time, then shut down,
        warning of the spans that couldn't be exported."""
        flushing = threading.Thread(target=self._flush, name="telemetry flush", daemon=True)
        flushing.start()
        flushing.join(self._flush_seconds)
        dropped = self._counts.dropped
        if dropped:
            logger.warning("Telemetry dropped %d spans it couldn't export.", dropped)

    def _flush(self) -> None:
        self._provider.force_flush(int(self._flush_seconds * 1000))
        self._provider.shutdown()


def _context(parent: Span) -> Context:
    """The OpenTelemetry context whose span is ``parent``, if it's one of OpenTelemetry's; an
    empty one otherwise, so a span started in it starts a trace."""
    if isinstance(parent, OtelSpan):
        return trace.set_span_in_context(parent.otel, Context())
    return Context()


class SpanCounts:
    """How many spans ended, and how many of them were exported."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ended = 0
        self._exported = 0

    def ended(self) -> None:
        with self._lock:
            self._ended += 1

    def exported(self, count: int) -> None:
        with self._lock:
            self._exported += count

    @property
    def dropped(self) -> int:
        """How many spans ended but weren't exported."""
        with self._lock:
            return self._ended - self._exported


class _Ending(SpanProcessor):
    """Counts the spans that end in ``counts``."""

    def __init__(self, counts: SpanCounts) -> None:
        self._counts = counts

    def on_end(self, span: ReadableSpan) -> None:
        self._counts.ended()


class _Counted(SpanExporter):
    """``exporter``, counting the spans it exports in ``counts``."""

    def __init__(self, exporter: SpanExporter, counts: SpanCounts) -> None:
        self._exporter = exporter
        self._counts = counts

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        result = self._exporter.export(spans)
        if result is SpanExportResult.SUCCESS:
            self._counts.exported(len(spans))
        return result

    def shutdown(self) -> None:
        self._exporter.shutdown()


def otel_telemetry(
    exporter: SpanExporter, resource: Resource, flush_seconds: float = FLUSH_SECONDS
) -> OtelTelemetry:
    """Telemetry describing ``resource``, every span batched to ``exporter``, with no
    sampling."""
    provider = TracerProvider(sampler=ALWAYS_ON, resource=resource, shutdown_on_exit=False)
    counts = SpanCounts()
    provider.add_span_processor(_Ending(counts))
    provider.add_span_processor(BatchSpanProcessor(_Counted(exporter, counts)))
    return OtelTelemetry(provider, counts, flush_seconds)
