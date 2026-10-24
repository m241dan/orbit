"""Spans written as OTLP JSON, one ``ExportTraceServiceRequest`` per line, as the Collector's
``otlpjsonfile`` receiver reads them."""

import base64
import json
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.protobuf.json_format import MessageToDict
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

logger = logging.getLogger(__name__)

_IDS = ("traceId", "spanId", "parentSpanId")
"""The fields of spans and links OTLP JSON writes in hex, where protobuf's JSON has base64."""


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Rotation:
    """How span files rotate: a new one before a line would take the current one past
    ``max_bytes``, keeping the newest ``keep``."""

    max_bytes: int
    keep: int


class OtlpJsonFileExporter(SpanExporter):
    """Writes spans to ``spans-*.jsonl`` files in ``directory``, rotated as ``rotation`` says,
    each named for when ``clock`` says it was started."""

    def __init__(
        self, directory: Path, rotation: Rotation, clock: Callable[[], datetime] = _now
    ) -> None:
        self._directory = directory
        self._rotation = rotation
        self._clock = clock
        self._current: Path | None = None

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Write ``spans`` as one line; a failure to write is a failed export, logged as a
        warning, as the SDK doesn't log it."""
        line = (json.dumps(otlp_json(spans), separators=(",", ":")) + "\n").encode()
        try:
            self._append(line)
        except OSError as exc:
            logger.warning("Telemetry couldn't write spans to %s: %s", self._directory, exc)
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        """Nothing is held open between exports."""

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Every export is written before it returns, so there's nothing to flush."""
        return True

    def _append(self, line: bytes) -> None:
        """Append ``line`` to the current file, or a new one if it would grow too big."""
        path = self._file_for(len(line))
        with path.open("ab") as file:
            file.write(line)

    def _file_for(self, size: int) -> Path:
        """The file a line of ``size`` bytes goes in, a new one if the current one is missing,
        or would grow past the limit with it."""
        current = self._current
        if current is None or not current.exists():
            return self._started()
        if current.stat().st_size + size > self._rotation.max_bytes:
            return self._started()
        return current

    def _started(self) -> Path:
        """A new, empty file, named for now, the oldest ones removed so it's among the newest
        kept."""
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = self._clock().strftime("%Y%m%dT%H%M%S%fZ")
        self._current = self._directory / f"spans-{stamp}-{os.getpid()}.jsonl"
        self._current.touch(mode=0o600)
        for old in sorted(self._directory.glob("spans-*.jsonl"))[: -self._rotation.keep]:
            old.unlink(missing_ok=True)
        return self._current


def otlp_json(spans: Sequence[ReadableSpan]) -> dict[str, Any]:
    """``spans`` as an OTLP JSON ``ExportTraceServiceRequest``."""
    request = MessageToDict(encode_spans(spans))
    for resource in request.get("resourceSpans", []):
        for scope in resource.get("scopeSpans", []):
            for span in scope.get("spans", []):
                _hex_ids(span)
                for link in span.get("links", []):
                    _hex_ids(link)
    return request


def _hex_ids(item: dict[str, Any]) -> None:
    """Write ``item``'s ids in hex, as OTLP JSON does, rather than protobuf JSON's base64."""
    for key in _IDS:
        if key in item:
            item[key] = base64.b64decode(item[key]).hex()
