"""The OpenTelemetry pipeline the telemetry settings ask for: its exporter and its resource."""

import socket
import sys
import uuid

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SpanExporter

from orbit import __version__
from orbit.telemetry.file_exporter import OtlpJsonFileExporter, Rotation
from orbit.telemetry.otel import OtelTelemetry, otel_telemetry
from orbit.telemetry.run import TelemetryRun
from orbit.telemetry.settings import OtlpTarget, TelemetrySettings

MEGABYTE = 1024 * 1024


def started(
    settings: TelemetrySettings, run: TelemetryRun, target: OtlpTarget, flush_seconds: float
) -> OtelTelemetry:
    """Telemetry exporting as ``settings`` say, over OTLP to ``target``, for orbit running as
    ``run`` says, flushing for at most ``flush_seconds`` on exit."""
    exporter = _exporter(settings, run, target, flush_seconds)
    return otel_telemetry(exporter, resource(settings, run), flush_seconds)


def resource(settings: TelemetrySettings, run: TelemetryRun) -> Resource:
    """What the spans of orbit running as ``run`` says describe it as: its service name,
    version and namespace, an instance id new each start, and the host's name."""
    return Resource.create(
        {
            "service.name": settings.service_name,
            "service.version": __version__,
            "service.namespace": run.namespace,
            "service.instance.id": str(uuid.uuid4()),
            "host.name": socket.gethostname(),
        }
    )


def _exporter(
    settings: TelemetrySettings, run: TelemetryRun, target: OtlpTarget, timeout: float
) -> SpanExporter:
    """The exporter ``settings`` name: to stderr, to files in the state directory, or over OTLP
    to ``target``, giving up on a request after ``timeout`` seconds."""
    if settings.exporter == "console":
        return ConsoleSpanExporter(service_name=settings.service_name, out=sys.stderr)
    if settings.exporter == "file":
        directory = run.state_dir / "telemetry"
        rotation = Rotation(settings.file_max_mb * MEGABYTE, settings.file_keep)
        return OtlpJsonFileExporter(directory, rotation)
    if settings.protocol == "grpc":
        return _grpc(target, timeout)
    return _http(target, timeout)


def _http(target: OtlpTarget, timeout: float) -> SpanExporter:
    """An exporter sending spans to ``target`` over OTLP/HTTP with protobuf."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(endpoint=target.endpoint, headers=dict(target.headers), timeout=timeout)


def _grpc(target: OtlpTarget, timeout: float) -> SpanExporter:
    """An exporter sending spans to ``target`` over OTLP/gRPC, which takes header names in
    lowercase."""
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    headers = {name.lower(): value for name, value in target.headers.items()}
    return OTLPSpanExporter(endpoint=target.endpoint, headers=headers, timeout=timeout)
