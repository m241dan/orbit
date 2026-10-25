"""Telemetry exporting its spans to memory, and the trace context naming them."""

from opentelemetry.sdk.metrics.export import (
    Gauge,
    InMemoryMetricReader,
    Metric,
    MetricsData,
    NumberDataPoint,
    Sum,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from orbit.agent.application.command_trace import COMMAND_SPAN
from orbit.telemetry.otel import OtelTelemetry, otel_telemetry

GaugePoint = tuple[dict[str, object], float]
"""A gauge's or a counter's point: its attributes and its value."""


class Traced:
    """Telemetry exporting its spans to memory, and recording metrics there."""

    def __init__(self) -> None:
        self.exporter = InMemorySpanExporter()
        self.metrics = InMemoryMetricReader()
        self.telemetry: OtelTelemetry = otel_telemetry(
            self.exporter, self.metrics, Resource.create({"service.name": "orbit-tests"})
        )

        self._closed = False

    def spans(self, name: str) -> list[ReadableSpan]:
        """The spans called ``name`` exported, once every span is."""
        if not self._closed:
            self._closed = True
            self.telemetry.close()
        return [span for span in self.exporter.get_finished_spans() if span.name == name]

    def gauge(self, name: str) -> list[GaugePoint]:
        """The points of the gauge called ``name``, as metrics are collected now; none if it has
        none."""
        gauges = _gauges(_metrics(self.metrics.get_metrics_data()), name)
        return [_point(point) for gauge in gauges for point in gauge.data_points]

    def counter(self, name: str) -> list[GaugePoint]:
        """The points of the counter called ``name``, as metrics are collected now; none if it
        has none."""
        metrics = _metrics(self.metrics.get_metrics_data())
        sums = [metric.data for metric in metrics if metric.name == name]
        return [
            _point(point) for each in sums if isinstance(each, Sum) for point in each.data_points
        ]

    def task(self) -> ReadableSpan:
        """The one task's span."""
        [task] = self.spans("orbit.task")
        return task

    def command(self, kind: str) -> ReadableSpan:
        """The one ``orbit.command`` span of a command of type ``kind``."""
        commands = self.spans(COMMAND_SPAN)
        [span] = [
            span for span in commands if (span.attributes or {})["orbit.command.type"] == kind
        ]
        return span


def _metrics(data: MetricsData | None) -> list[Metric]:
    """Every metric ``data`` holds; none without data."""
    resources = data.resource_metrics if data is not None else []
    scopes = [scope for resource in resources for scope in resource.scope_metrics]
    return [metric for scope in scopes for metric in scope.metrics]


def _gauges(metrics: list[Metric], name: str) -> list[Gauge]:
    """The gauges among ``metrics`` called ``name``."""
    data = [metric.data for metric in metrics if metric.name == name]
    return [gauge for gauge in data if isinstance(gauge, Gauge)]


def _point(point: NumberDataPoint) -> GaugePoint:
    """``point``'s attributes and value."""
    return dict(point.attributes or {}), point.value


def linked_to(span: ReadableSpan, target: ReadableSpan) -> bool:
    """Whether ``span`` links to ``target``, and to nothing else."""
    assert target.context is not None
    return [link.context.span_id for link in span.links] == [target.context.span_id]


def trace_id(span: ReadableSpan) -> int:
    """The id of ``span``'s trace."""
    assert span.context is not None
    return int(span.context.trace_id)


def traceparent_of(span: ReadableSpan) -> str:
    """The W3C ``traceparent`` naming ``span``, with its flags, as an envelope carries it."""
    context = span.context
    assert context is not None
    return f"00-{context.trace_id:032x}-{context.span_id:016x}-{context.trace_flags:02x}"
