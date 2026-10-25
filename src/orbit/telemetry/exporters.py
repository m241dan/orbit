"""The OpenTelemetry pipeline the telemetry settings ask for: its exporters and its resource."""

import socket
import sys
import uuid
from collections.abc import Mapping

from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    MetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SpanExporter

from orbit import __version__
from orbit.telemetry.file_exporter import (
    OtlpJsonFileExporter,
    OtlpJsonMetricsFileExporter,
    Rotation,
)
from orbit.telemetry.otel import OtelTelemetry, otel_telemetry
from orbit.telemetry.run import TelemetryRun
from orbit.telemetry.settings import OtlpTarget, TelemetrySettings, otlp_target

MEGABYTE = 1024 * 1024

Exporters = tuple[SpanExporter, MetricExporter]
"""Where spans go, and where metrics go."""


def started(
    settings: TelemetrySettings,
    run: TelemetryRun,
    headers: Mapping[str, str],
    flush_seconds: float,
) -> OtelTelemetry:
    """Telemetry exporting as ``settings`` say, over OTLP with the headers file's ``headers``,
    for orbit running as ``run`` says, flushing for at most ``flush_seconds`` on exit."""
    spans, metrics = _exporters(settings, run, headers, flush_seconds)
    reader = PeriodicExportingMetricReader(
        metrics,
        export_interval_millis=settings.metrics_interval_s * 1000,
        export_timeout_millis=flush_seconds * 1000,
    )
    return otel_telemetry(spans, reader, resource(settings, run), flush_seconds)


def resource(settings: TelemetrySettings, run: TelemetryRun) -> Resource:
    """What the spans and metrics of orbit running as ``run`` says describe it as: its service
    name, version and namespace, an instance id new each start, and the host's name."""
    return Resource.create(
        {
            "service.name": settings.service_name,
            "service.version": __version__,
            "service.namespace": run.namespace,
            "service.instance.id": str(uuid.uuid4()),
            "host.name": socket.gethostname(),
        }
    )


def _exporters(
    settings: TelemetrySettings, run: TelemetryRun, headers: Mapping[str, str], timeout: float
) -> Exporters:
    """The exporters ``settings`` name: to stderr, to files in the state directory, or over OTLP
    with ``headers``, giving up on a request after ``timeout`` seconds."""
    if settings.exporter == "console":
        spans = ConsoleSpanExporter(service_name=settings.service_name, out=sys.stderr)
        return spans, ConsoleMetricExporter(out=sys.stderr)
    if settings.exporter == "file":
        directory = run.state_dir / "telemetry"
        rotation = Rotation(settings.file_max_mb * MEGABYTE, settings.file_keep)
        return OtlpJsonFileExporter(directory, rotation), OtlpJsonMetricsFileExporter(
            directory, rotation
        )
    traces = otlp_target(settings, headers, run.environ, "traces")
    metrics = otlp_target(settings, headers, run.environ, "metrics")
    if settings.protocol == "grpc":
        return _grpc(traces, metrics, timeout)
    return _http(traces, metrics, timeout)


def _http(traces: OtlpTarget, metrics: OtlpTarget, timeout: float) -> Exporters:
    """Exporters sending spans to ``traces`` and metrics to ``metrics`` over OTLP/HTTP with
    protobuf."""
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return (
        OTLPSpanExporter(endpoint=traces.endpoint, headers=dict(traces.headers), timeout=timeout),
        OTLPMetricExporter(
            endpoint=metrics.endpoint, headers=dict(metrics.headers), timeout=timeout
        ),
    )


def _grpc(traces: OtlpTarget, metrics: OtlpTarget, timeout: float) -> Exporters:
    """Exporters sending spans to ``traces`` and metrics to ``metrics`` over OTLP/gRPC."""
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    return (
        OTLPSpanExporter(endpoint=traces.endpoint, headers=_lower(traces), timeout=timeout),
        OTLPMetricExporter(endpoint=metrics.endpoint, headers=_lower(metrics), timeout=timeout),
    )


def _lower(target: OtlpTarget) -> dict[str, str]:
    """``target``'s headers, named in lowercase, as gRPC takes them."""
    return {name.lower(): value for name, value in target.headers.items()}
