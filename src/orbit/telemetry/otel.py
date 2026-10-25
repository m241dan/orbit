"""The tracing port over the OpenTelemetry SDK: spans batched to an exporter, the current span
following orbit's own, the trace context carried in envelopes, metrics recorded from the spans,
and live gauges."""

import logging
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.metrics import AlwaysOffExemplarFilter, MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import Link, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from orbit.protocol import trace_carrier, with_trace_carrier
from orbit.telemetry.gauges import OtelGauges
from orbit.telemetry.metrics import MetricsFromSpans
from orbit.telemetry.run import FLUSH_SECONDS
from orbit.tracing import (
    Attributes,
    AttributeValue,
    Carrier,
    Span,
    current_span,
    span_block,
    use_span,
)

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


class OtelTelemetry:
    """Telemetry over ``provider``, whose spans nest under orbit's current span, and ``meters``,
    which exports the metrics recorded from them and the :attr:`gauges` registered; ``counts``
    tells how many spans it couldn't export, and :meth:`close` waits at most ``flush_seconds``
    for the spans and metrics not yet exported."""

    def __init__(
        self,
        provider: TracerProvider,
        meters: MeterProvider,
        counts: SpanCounts,
        flush_seconds: float = FLUSH_SECONDS,
    ) -> None:
        self._provider = provider
        self._meters = meters
        self._tracer = provider.get_tracer("orbit")
        self._gauges = OtelGauges(meters.get_meter("orbit"))
        self._counts = counts
        self._flush_seconds = flush_seconds

    @property
    def gauges(self) -> OtelGauges:
        """What the live gauges are registered with, exported with the metrics."""
        return self._gauges

    def span(
        self, name: str, attributes: Attributes | None = None, links: Sequence[Span] = ()
    ) -> AbstractContextManager[OtelSpan]:
        """A span called ``name``, linked to each of ``links``, current for the block and ended
        on leaving it."""
        return span_block(self.start(name, attributes, links))

    def start(
        self, name: str, attributes: Attributes | None = None, links: Sequence[Span] = ()
    ) -> OtelSpan:
        """A span called ``name``, a child of the current span and linked to each of ``links``,
        running until its ``end``."""
        otel = self._tracer.start_span(
            name, context=_context(current_span()), attributes=attributes, links=_links(links)
        )
        return OtelSpan(otel)

    def carry[E: Carrier](self, envelope: E) -> E:
        """``envelope`` carrying the current span's trace context."""
        carrier: dict[str, str] = {}
        _PROPAGATOR.inject(carrier, context=_context(current_span()))
        return with_trace_carrier(envelope, carrier)

    def carried(self, envelope: Carrier) -> OtelSpan:
        """The remote span whose trace context ``envelope`` carries, to make current so the
        spans started under it are its children, or to link to; one recording nothing if it
        carries none."""
        context = _PROPAGATOR.extract(trace_carrier(envelope))
        return OtelSpan(trace.get_current_span(context))

    def close(self) -> None:
        """Export the spans and metrics not yet exported, side by side, waiting at most the
        flush time for both, then shut down, warning of the spans that couldn't be exported."""
        deadline = time.monotonic() + self._flush_seconds
        flushes = [_flushing(self._flush_spans, "spans"), _flushing(self._flush_metrics, "metrics")]
        for flushing in flushes:
            flushing.join(max(deadline - time.monotonic(), 0))
        dropped = self._counts.dropped
        if dropped:
            logger.warning("Telemetry dropped %d spans it couldn't export.", dropped)

    def _flush_spans(self) -> None:
        self._provider.force_flush(int(self._flush_seconds * 1000))
        self._provider.shutdown()

    def _flush_metrics(self) -> None:
        """Shut the meters down, which exports the metrics not yet exported first."""
        self._meters.shutdown(self._flush_seconds * 1000)


def _flushing(flush: Callable[[], None], what: str) -> threading.Thread:
    """A thread started running ``flush``, which flushes ``what``; it won't keep orbit from
    exiting."""
    thread = threading.Thread(target=flush, name=f"telemetry {what} flush", daemon=True)
    thread.start()
    return thread


def _context(parent: Span) -> Context:
    """The OpenTelemetry context whose span is ``parent``, if it's one of OpenTelemetry's; an
    empty one otherwise, so a span started in it starts a trace."""
    if isinstance(parent, OtelSpan):
        return trace.set_span_in_context(parent.otel, Context())
    return Context()


def _links(spans: Sequence[Span]) -> list[Link]:
    """Links to those of ``spans`` that are OpenTelemetry's: the others record nothing to link
    to."""
    return [Link(span.otel.get_span_context()) for span in spans if isinstance(span, OtelSpan)]


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
    exporter: SpanExporter,
    reader: MetricReader,
    resource: Resource,
    flush_seconds: float = FLUSH_SECONDS,
) -> OtelTelemetry:
    """Telemetry describing ``resource``, every span batched to ``exporter``, with no sampling,
    and the metrics recorded from them read by ``reader``."""
    # Orbit keeps its own current span, not OpenTelemetry's, so exemplars would name no span.
    meters = MeterProvider(
        metric_readers=[reader],
        resource=resource,
        exemplar_filter=AlwaysOffExemplarFilter(),
        shutdown_on_exit=False,
    )
    provider = TracerProvider(sampler=ALWAYS_ON, resource=resource, shutdown_on_exit=False)
    counts = SpanCounts()
    provider.add_span_processor(_Ending(counts))
    provider.add_span_processor(MetricsFromSpans(meters.get_meter("orbit")))
    provider.add_span_processor(BatchSpanProcessor(_Counted(exporter, counts)))
    return OtelTelemetry(provider, meters, counts, flush_seconds)
