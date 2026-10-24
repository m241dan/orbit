"""Starting the telemetry the settings ask for, and what each exporter does with spans."""

import json
import logging
import socket
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, ClassVar

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Link

from orbit import __version__
from orbit.telemetry import (
    FLUSH_SECONDS,
    HeadersFileError,
    Off,
    TelemetryNotInstalled,
    TelemetryRun,
    TelemetrySettings,
    start_telemetry,
)
from orbit.telemetry.file_exporter import OtlpJsonFileExporter, Rotation, otlp_json
from orbit.telemetry.otel import OtelTelemetry


def _run(tmp_path: Path, environ: dict[str, str] | None = None) -> TelemetryRun:
    return TelemetryRun("orbit-serve", tmp_path / "config", tmp_path / "state", environ or {})


def test_orbit_waits_five_seconds_at_most_for_spans_on_exit() -> None:
    assert FLUSH_SECONDS == 5.0


def test_with_no_exporter_telemetry_is_off_and_closes_doing_nothing(tmp_path: Path) -> None:
    telemetry = start_telemetry(TelemetrySettings(), _run(tmp_path))

    assert isinstance(telemetry, Off)
    with telemetry.span("orbit.task"):
        pass
    telemetry.close()


def test_with_no_exporter_the_sdk_is_never_imported(tmp_path: Path) -> None:
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "import orbit.cli\n"
        "from orbit.telemetry import TelemetryRun, TelemetrySettings, start_telemetry\n"
        "run = TelemetryRun('orbit-chat', Path('.'), Path('.'), {})\n"
        "start_telemetry(TelemetrySettings(), run).close()\n"
        "print(sorted(m for m in sys.modules if m.startswith(('opentelemetry.sdk', 'orbit.'))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, cwd=tmp_path
    )

    assert "opentelemetry.sdk" not in result.stdout
    assert "orbit.telemetry.otel" not in result.stdout


@pytest.mark.parametrize(
    ("table", "extra"),
    [
        ({"exporter": "otlp"}, "telemetry"),
        ({"exporter": "otlp", "protocol": "grpc"}, "telemetry-grpc"),
        ({"exporter": "file"}, "telemetry"),
    ],
)
def test_telemetry_configured_without_its_extra_names_the_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, table: dict[str, str], extra: str
) -> None:
    monkeypatch.setitem(sys.modules, "orbit.telemetry.exporters", None)
    settings = TelemetrySettings.model_validate(table)

    with pytest.raises(TelemetryNotInstalled) as raised:
        start_telemetry(settings, _run(tmp_path))
    assert str(raised.value) == (
        f"Telemetry is configured but not installed: pip install 'orbit[{extra}]'"
    )


def _lines(directory: Path) -> list[dict[str, Any]]:
    """The OTLP JSON requests in the span files in ``directory``, oldest first."""
    files = sorted(directory.glob("spans-*.jsonl"))
    return [json.loads(line) for path in files for line in path.read_text().splitlines()]


def _attributes(items: list[dict[str, Any]]) -> dict[str, Any]:
    """OTLP JSON ``items``' attributes, by key, their values unwrapped."""
    return {item["key"]: next(iter(item["value"].values())) for item in items}


def test_the_file_exporter_writes_otlp_json_lines_with_the_resource(tmp_path: Path) -> None:
    telemetry = start_telemetry(TelemetrySettings(exporter="file"), _run(tmp_path))
    with telemetry.span("orbit.task", {"orbit.task.id": "t1"}), telemetry.span("orbit.task.run"):
        pass
    telemetry.close()

    [request] = _lines(tmp_path / "state" / "telemetry")
    [resource_spans] = request["resourceSpans"]
    resource = _attributes(resource_spans["resource"]["attributes"])
    assert resource["service.name"] == "orbit"
    assert resource["service.version"] == __version__
    assert resource["service.namespace"] == "orbit-serve"
    assert resource["host.name"] == socket.gethostname()
    assert len(resource["service.instance.id"]) == 36
    [scope] = resource_spans["scopeSpans"]
    spans = {span["name"]: span for span in scope["spans"]}
    task, run = spans["orbit.task"], spans["orbit.task.run"]
    assert _attributes(task["attributes"]) == {"orbit.task.id": "t1"}
    assert len(task["traceId"]) == 32 and len(task["spanId"]) == 16
    assert "parentSpanId" not in task
    assert run["parentSpanId"] == task["spanId"]
    assert run["traceId"] == task["traceId"]


def test_each_start_is_a_new_instance_named_as_configured(tmp_path: Path) -> None:
    settings = TelemetrySettings(exporter="file", service_name="orbit-ci")
    for _ in range(2):
        telemetry = start_telemetry(settings, _run(tmp_path))
        with telemetry.span("orbit.task"):
            pass
        telemetry.close()

    resources = [
        _attributes(request["resourceSpans"][0]["resource"]["attributes"])
        for request in _lines(tmp_path / "state" / "telemetry")
    ]
    assert {resource["service.name"] for resource in resources} == {"orbit-ci"}
    assert len({resource["service.instance.id"] for resource in resources}) == 2


def test_otlp_json_writes_links_ids_in_hex() -> None:
    tracer = TracerProvider().get_tracer("tests")
    linked = tracer.start_span("orbit.review")
    span = tracer.start_span("orbit.task", links=[Link(linked.get_span_context())])
    span.end()
    assert isinstance(span, ReadableSpan)

    request = otlp_json([span])

    [link] = request["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["links"]
    assert link["spanId"] == f"{linked.get_span_context().span_id:016x}"
    assert link["traceId"] == f"{linked.get_span_context().trace_id:032x}"


class Ticking:
    """A clock a second later each time it's read."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 22, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def _ended(name: str) -> ReadableSpan:
    span = TracerProvider().get_tracer("tests").start_span(name)
    span.end()
    assert isinstance(span, ReadableSpan)
    return span


def test_the_file_exporter_rotates_past_its_size_keeping_the_newest_files(tmp_path: Path) -> None:
    line = len(json.dumps(otlp_json([_ended("orbit.task")]), separators=(",", ":"))) + 1
    exporter = OtlpJsonFileExporter(tmp_path, Rotation(2 * line + 10, keep=2), Ticking())

    results = [exporter.export([_ended(f"span-{n}")]) for n in range(7)]

    assert set(results) == {SpanExportResult.SUCCESS}
    files = sorted(tmp_path.glob("spans-*.jsonl"))
    assert len(files) == 2
    names = [
        request["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"]
        for request in _lines(tmp_path)
    ]
    assert names == ["span-4", "span-5", "span-6"]
    assert [len(path.read_text().splitlines()) for path in files] == [2, 1]


def test_the_file_exporter_starts_a_new_file_if_its_file_is_removed(tmp_path: Path) -> None:
    exporter = OtlpJsonFileExporter(tmp_path, Rotation(1_000_000, 5), Ticking())
    exporter.export([_ended("first")])
    [first] = tmp_path.glob("spans-*.jsonl")
    first.unlink()

    exporter.export([_ended("second")])

    [request] = _lines(tmp_path)
    assert request["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "second"


def test_the_span_files_are_readable_by_their_owner_alone(tmp_path: Path) -> None:
    exporter = OtlpJsonFileExporter(tmp_path / "telemetry", Rotation(1_000_000, 5))
    exporter.export([_ended("orbit.task")])

    [path] = (tmp_path / "telemetry").glob("spans-*.jsonl")
    assert path.stat().st_mode & 0o077 == 0
    assert (tmp_path / "telemetry").stat().st_mode & 0o077 == 0


def test_a_file_export_that_cant_be_written_fails(tmp_path: Path) -> None:
    blocked = tmp_path / "telemetry"
    blocked.write_text("a file, not a directory")
    exporter = OtlpJsonFileExporter(blocked, Rotation(1_000_000, 5))

    assert exporter.export([_ended("orbit.task")]) is SpanExportResult.FAILURE
    assert exporter.force_flush()
    exporter.shutdown()


def test_a_file_export_that_cant_be_written_logs_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocked = tmp_path / "telemetry"
    blocked.write_text("a file, not a directory")
    exporter = OtlpJsonFileExporter(blocked, Rotation(1_000_000, 5))

    with caplog.at_level(logging.WARNING):
        result = exporter.export([_ended("orbit.task")])

    assert result is SpanExportResult.FAILURE
    [record] = caplog.records
    assert (record.name, record.levelno) == ("orbit.telemetry.file_exporter", logging.WARNING)
    assert record.getMessage().startswith(f"Telemetry couldn't write spans to {blocked}: ")


def test_the_console_exporter_writes_spans_to_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    telemetry = start_telemetry(TelemetrySettings(exporter="console"), _run(tmp_path))
    with telemetry.span("orbit.task"):
        pass
    telemetry.close()

    captured = capsys.readouterr()
    assert '"name": "orbit.task"' in captured.err
    assert captured.out == ""


@dataclass
class Received:
    """What a collector received: each request's path and authorization header, and the spans'
    names."""

    paths: list[str] = field(default_factory=list)
    authorization: list[str | None] = field(default_factory=list)
    spans: list[str] = field(default_factory=list)


def _handler(received: Received) -> type[BaseHTTPRequestHandler]:
    class Collector(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            request = ExportTraceServiceRequest.FromString(body)
            received.paths.append(self.path)
            received.authorization.append(self.headers.get("Authorization"))
            for resource in request.resource_spans:
                for scope in resource.scope_spans:
                    received.spans += [span.name for span in scope.spans]
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            """Log nothing."""

    return Collector


@pytest.fixture
def collector() -> Iterator[tuple[str, Received]]:
    """A collector receiving OTLP over HTTP, its base URL, and what it received."""
    received = Received()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(received))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", received
    server.shutdown()
    server.server_close()


def _closed_port() -> int:
    """A port nothing listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def _span_through(telemetry: object) -> None:
    assert isinstance(telemetry, OtelTelemetry)
    with telemetry.span("orbit.task"):
        pass
    telemetry.close()


def _write_headers(tmp_path: Path, mode: int = 0o600) -> None:
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    path = config / "otel-headers"
    path.write_text("# The collector's key.\nAuthorization: Bearer abc\n")
    path.chmod(mode)


def test_otlp_sends_spans_to_the_collector_with_the_headers_files_headers(
    tmp_path: Path, collector: tuple[str, Received]
) -> None:
    url, received = collector
    _write_headers(tmp_path)
    settings = TelemetrySettings(exporter="otlp", endpoint=url, headers_file="otel-headers")

    _span_through(start_telemetry(settings, _run(tmp_path)))

    assert received.paths == ["/v1/traces"]
    assert received.authorization == ["Bearer abc"]
    assert received.spans == ["orbit.task"]


def test_the_environments_endpoint_overrides_the_configured_one(
    tmp_path: Path, collector: tuple[str, Received]
) -> None:
    url, received = collector
    settings = TelemetrySettings(exporter="otlp", endpoint=f"http://127.0.0.1:{_closed_port()}")
    run = _run(tmp_path, {"OTEL_EXPORTER_OTLP_ENDPOINT": url})

    _span_through(start_telemetry(settings, run))

    assert received.spans == ["orbit.task"]


def test_a_headers_file_others_can_read_stops_telemetry_starting(tmp_path: Path) -> None:
    _write_headers(tmp_path, 0o644)
    settings = TelemetrySettings(exporter="otlp", headers_file="otel-headers")

    with pytest.raises(HeadersFileError, match="run chmod 600"):
        start_telemetry(settings, _run(tmp_path))


def test_the_headers_file_is_read_only_for_otlp(tmp_path: Path) -> None:
    settings = TelemetrySettings(exporter="file", headers_file="missing")

    start_telemetry(settings, _run(tmp_path)).close()


def test_closing_with_an_unreachable_collector_returns_in_time_warning_of_the_drop(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = TelemetrySettings(exporter="otlp", endpoint=f"http://127.0.0.1:{_closed_port()}")
    telemetry = start_telemetry(settings, _run(tmp_path), flush_seconds=0.5)
    with telemetry.span("orbit.task"):
        pass

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="orbit.telemetry"):
        telemetry.close()

    assert time.monotonic() - started < 2
    dropped = [r.getMessage() for r in caplog.records if r.name == "orbit.telemetry.otel"]
    assert dropped == ["Telemetry dropped 1 spans it couldn't export."]


class Recorded(SpanExporter):
    """A stand-in for the gRPC exporter, recording how it was made."""

    made: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, **kwargs: Any) -> None:
        Recorded.made.append(kwargs)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return SpanExportResult.SUCCESS


def test_otlp_over_grpc_sends_lowercase_headers_to_the_endpoint_as_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter", Recorded
    )
    Recorded.made = []
    _write_headers(tmp_path)
    settings = TelemetrySettings(
        exporter="otlp", protocol="grpc", endpoint="collector:4317", headers_file="otel-headers"
    )

    start_telemetry(settings, _run(tmp_path)).close()

    assert Recorded.made == [
        {"endpoint": "collector:4317", "headers": {"authorization": "Bearer abc"}, "timeout": 5.0}
    ]
